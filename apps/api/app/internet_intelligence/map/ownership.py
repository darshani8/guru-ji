"""Owner confirmation: the institution itself says which accounts are its own.

Everything else in the map is inferred; this is the one thing the owner
states. An institution proves it controls one of its own domains (one it
configured, or a reviewer confirmed) with that domain's token, published in
any one of three places:

* a meta tag on the homepage: ``<meta name="guruji-verification" content="TOKEN">``;
* a DNS TXT record on the domain: ``guruji-verification=TOKEN``;
* a file at ``https://DOMAIN/.well-known/guruji.json``:
  ``{"verification": "TOKEN", "accounts": ["https://www.instagram.com/...", ...]}``.

A verified domain is graded O. Accounts listed in the verified file are O
too (the owner vouches for them by name); an account taken off the list is
withdrawn and falls back to whatever else supports it. The token is keyed
to the institution and the domain, so another tenant cannot use it, and a
copy on any other host (a taken-over subdomain, a look-alike) proves nothing.
Domains that are official only by inference are never checked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .assets import ACCOUNT, GROUP, asset_ref
from .connectors.base import ConnectorContext, ConnectorResult, Lead
from .connectors.common import FAILED_OUTCOMES, ApiClient
from .connectors.infrastructure import resolve
from .pipeline import regrade
from .structure import parse_structure

WELL_KNOWN_PATH = "/.well-known/guruji.json"
META_NAME = "guruji-verification"
MAX_LISTED_ACCOUNTS = 200


def verification_token(key: bytes, institution_id: str, host: str) -> str:
    """The ownership token for one domain of one institution (stable and keyed).

    Binding it to the host means a token copied from the institution's site
    proves nothing on any other host (a taken-over subdomain, a look-alike).
    """

    domain = host.strip().lower().removeprefix("www.")
    return "gj-" + hmac.new(key, f"ownership:{institution_id}:{domain}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _matches(value: Any, token: str) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def instructions(key: bytes, institution_id: str, domains: list[str]) -> dict[str, Any]:
    """How to confirm each of the institution's own domains; every domain has its own token."""

    return {
        "domains": [
            {
                "domain": domain, "token": (token := verification_token(key, institution_id, domain)),
                "methods": [
                    {"method": "meta_tag", "how": f'Add <meta name="{META_NAME}" content="{token}"> inside <head> on https://{domain}/'},
                    {"method": "dns_txt", "how": f"Add a DNS TXT record on {domain}: {META_NAME}={token}"},
                    {"method": "well_known_file", "how": f"Publish https://{domain}{WELL_KNOWN_PATH}", "example": {"verification": token, "accounts": ["https://www.instagram.com/<your_account>/", "https://www.youtube.com/@<your_channel>"]}},
                ],
            }
            for domain in domains
        ],
        "notes": [
            "Each domain has its own token; a token proves nothing on any other host.",
            "Any one method proves the domain; only the well-known file can also name accounts, which are then graded O (confirmed by the owner).",
            "Only domains the institution configured (or a reviewer confirmed) are listed and checked.",
            "Remove an account from the file to withdraw it; the map re-checks weekly.",
        ],
    }


@dataclass(slots=True)
class OwnerClaimsConnector:
    """Check each official domain for the institution's token and read its account list (target: the domain's asset id)."""

    key: bytes = field(default=b"guru-ji-development-only", repr=False)
    dns: ApiClient | None = None  # DNS TXT is checked only when the dns connector is allowed
    name: str = "owner_claims"
    access_mode: str = "owner"
    budget_key: str = "fetch"
    max_grade: str = "O"
    default_interval: int = 7 * 86400

    def enabled(self) -> bool:
        return True

    def cost(self, source: Mapping[str, Any]) -> float:
        return 2.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        return [
            Lead(self.name, domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.default_interval)
            for domain in owned_domains(context.store, context.institution_id) if domain["asset_id"] not in existing
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        domain = context.store.get_asset(context.institution_id, str(source["target"]))
        if domain is None or domain["kind"] != "domain":
            return ConnectorResult(outcome="asset_missing", prune=True)
        if not nominated_by_institution(context.store, context.institution_id, domain["asset_id"]):
            # Official only by inference: the owner check is not offered there.
            return ConnectorResult(outcome="not_nominated", prune=True)
        host = domain["asset_key"].removeprefix("web:")
        token = verification_token(self.key, context.institution_id, host)
        proofs: list[str] = []
        homepage = await context.fetcher.retrieve(f"https://{host}/")
        if homepage.ok and _matches(parse_structure(homepage.text, homepage.url).meta.get(META_NAME), token):
            proofs.append("meta_tag")
        if self.dns is not None:
            outcome, records = await resolve(self.dns, host, "TXT")
            if outcome == "ok" and any(_matches(record.removeprefix(f"{META_NAME}="), token) for record in records if record.startswith(f"{META_NAME}=")):
                proofs.append("dns_txt")
        listed = await self._well_known(context, host, token)
        if listed is not None:
            proofs.append("well_known_file")
        result = ConnectorResult(outcome="verified" if proofs else ("no_proof" if homepage.outcome not in FAILED_OUTCOMES else homepage.outcome), touched={domain["asset_id"]})
        self._record(context, domain["asset_id"], host, proofs, f"https://{host}/")
        if listed is not None:
            self._accounts(context, domain, listed, result)
        regrade(context.store, context.institution_id, sorted(result.touched))
        return result

    async def _well_known(self, context: ConnectorContext, host: str, token: str) -> list[str] | None:
        """The accounts a verified file lists; None when there is no file or its token is wrong."""

        retrieval = await context.fetcher.retrieve(f"https://{host}{WELL_KNOWN_PATH}", accept=frozenset({"json", "text"}), max_bytes=200_000)  # type: ignore[union-attr]
        if not retrieval.ok:
            return None
        try:
            payload = json.loads(retrieval.body)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not _matches(payload.get("verification"), token):
            return None
        accounts = payload.get("accounts")
        return [str(item) for item in accounts if isinstance(item, str)][:MAX_LISTED_ACCOUNTS] if isinstance(accounts, list) else []

    @staticmethod
    def _latest_owner_claims(context: ConnectorContext, asset_ids: list[str], channel: str) -> dict[str, str]:
        latest: dict[str, str] = {}
        for asset_id, rows in context.store.evidence_for(context.institution_id, asset_ids).items():
            for row in rows:
                if row["kind"] == "owner_claim" and row["channel"] == channel:
                    latest[asset_id] = row["polarity"]
        return latest

    def _record(self, context: ConnectorContext, asset_id: str, host: str, proofs: list[str], url: str) -> None:
        channel = f"owner:{host}"
        previous = self._latest_owner_claims(context, [asset_id], channel).get(asset_id)
        if proofs:
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", detail=f"domain verified by {', '.join(proofs)}", source_url=url, channel=channel, observed_via="owner", run_id=context.run_id)
        elif previous == "supports":
            # The token is gone: the owner's confirmation is withdrawn.
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", polarity="refutes", detail="verification token no longer published", source_url=url, channel=channel, observed_via="owner", run_id=context.run_id)

    def _accounts(self, context: ConnectorContext, domain: Mapping[str, Any], listed: list[str], result: ConnectorResult) -> None:
        host = domain["asset_key"].removeprefix("web:")
        channel = f"owner-file:{host}"
        source_url = f"https://{host}{WELL_KNOWN_PATH}"
        named: set[str] = set()
        for url in listed:
            try:
                ref = asset_ref(url)
            except ValueError:
                continue
            if ref.kind not in {ACCOUNT, GROUP} or context.store.is_suppressed(context.institution_id, ref.key):
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=domain["entity_id"], relation="official", note=f"listed by the owner on {host}")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", detail=f"listed in {WELL_KNOWN_PATH}", source_url=source_url, source_asset_id=domain["asset_id"], channel=channel, observed_via="owner", run_id=context.run_id)
            named.add(asset_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        # An account the owner listed before and has taken off the list is withdrawn.
        before = [row["asset_id"] for row in context.store.links_from(context.institution_id, domain["asset_id"], kind="owner_claim")]
        for asset_id, polarity in self._latest_owner_claims(context, list(dict.fromkeys(before)), channel).items():
            if polarity == "supports" and asset_id not in named:
                context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", polarity="refutes", detail=f"no longer listed in {WELL_KNOWN_PATH}", source_url=source_url, source_asset_id=domain["asset_id"], channel=channel, observed_via="owner", run_id=context.run_id)
                result.touched.add(asset_id)


def nominated_by_institution(store: Any, institution_id: str, asset_id: str) -> bool:
    """A domain the institution configured or a reviewer confirmed (an earlier owner proof counts too)."""

    return any(
        row["polarity"] == "supports" and row["kind"] in {"configured_domain", "reviewer_confirm", "owner_claim"}
        for row in store.evidence_for(institution_id, [asset_id])[asset_id]
    )


def owned_domains(store: Any, institution_id: str) -> list[dict[str, Any]]:
    return [domain for domain in store.iter_assets(institution_id, kind="domain", relation="official") if nominated_by_institution(store, institution_id, domain["asset_id"])]


__all__ = ["META_NAME", "OwnerClaimsConnector", "WELL_KNOWN_PATH", "instructions", "nominated_by_institution", "owned_domains", "verification_token"]
