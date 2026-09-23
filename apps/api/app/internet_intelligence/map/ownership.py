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
withdrawn and falls back to whatever else supports it, and so is every listed
account when the file is deleted, its token breaks or the domain is lost. The
token is keyed to the institution and the domain, so another tenant cannot
use it, and a copy on any other host (a taken-over subdomain, a look-alike)
proves nothing. Domains that are official only by inference are never checked.

The token is public, so it is the site speaking, not necessarily the owner:
a compromised, hijacked, parked or redirected site is not checked at all, a
reviewer's rejection or a detected hijack is never lifted by it (only a
reviewer can), and a manager can rotate the institution's tokens so every old
copy stops proving anything. Only a plain answer withdraws a confirmation (a
homepage without the tag, a file that is gone or carries no valid token, DNS
without the record); an outage, a block or robots.txt leaves it as it was.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..fetch import HTML_TYPES, Retrieval
from .assets import ACCOUNT, GROUP, asset_ref
from .connectors.base import ConnectorContext, ConnectorResult, Lead
from .connectors.common import ApiClient
from .connectors.infrastructure import resolve
from .integrity import CLEAN, assess
from .pipeline import regrade, withdraw_owner_listing
from .structure import parse_structure

WELL_KNOWN_PATH = "/.well-known/guruji.json"
META_NAME = "guruji-verification"
MAX_LISTED_ACCOUNTS = 200
# A site in one of these states is not the owner speaking: whoever controls its pages now could publish the (public) token.
UNHEALTHY_STATUSES = frozenset({"compromised", "hijacked", "parked", "redirected"})
# Plain answers that the file is not there (a 200 page of another type is a soft 404).
_FILE_ABSENT = frozenset({"not_found", "gone", "content_type"})


def verification_token(key: bytes, institution_id: str, host: str, epoch: int = 0) -> str:
    """The ownership token for one domain of one institution at one epoch (stable and keyed).

    Binding it to the host means a token copied from the institution's site
    proves nothing on any other host (a taken-over subdomain, a look-alike).
    A lapsed or hijacked domain keeps serving the token it had, so a manager
    can move the institution to a new epoch and every older token stops
    proving anything; epoch 0 is the token issued before rotation existed,
    so tokens already published keep working until the first rotation.
    """

    domain = host.strip().lower().removeprefix("www.")
    message = f"ownership:{institution_id}:{domain}" + (f":{epoch}" if epoch else "")
    return "gj-" + hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _bare(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".").removeprefix("www.")


def _matches(value: Any, token: str) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def instructions(key: bytes, institution_id: str, domains: list[str], *, epoch: int = 0) -> dict[str, Any]:
    """How to confirm each of the institution's own domains; every domain has its own token."""

    return {
        "epoch": epoch,
        "domains": [
            {
                "domain": domain, "token": (token := verification_token(key, institution_id, domain, epoch)),
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
            "The token is public: if one may be in the wrong hands (a lapsed or hijacked domain), rotate the tokens and publish the new ones; confirmations resting on an old token are withdrawn at the next check.",
        ],
    }


@dataclass(slots=True)
class OwnerClaimsConnector:
    """Check each official domain for the institution's token and read its account list (target: the domain's asset id)."""

    key: bytes = field(default=b"guru-ji-development-only", repr=False)
    dns: ApiClient | None = None  # DNS over HTTPS for the TXT method (the platform always passes one; None skips it)
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
        if domain["status"] in UNHEALTHY_STATUSES:
            return ConnectorResult(outcome="unhealthy")
        host = domain["asset_key"].removeprefix("web:")
        token = verification_token(self.key, context.institution_id, host, context.store.owner_token_epoch(context.institution_id))
        home, home_outcome = await self._homepage(context, host, token)
        if home == "unhealthy":
            # Spam or a lander on the homepage: the site cannot speak for the owner, nor take a confirmation back.
            return ConnectorResult(outcome="unhealthy")
        dns_outcome, dns_found = await self._dns(host, token)
        listed = await self._well_known(context, domain, host, token)
        proofs = [name for name, found in (("meta_tag", home == "tag"), ("dns_txt", dns_found), ("well_known_file", isinstance(listed, list))) if found]
        # Withdrawn only on plain answers from every method: an outage proves nothing.
        absent = not proofs and home == "none" and listed == "absent" and dns_outcome in {"unchecked", "ok"}
        if not proofs and home is None and listed is None and dns_outcome != "ok":
            return ConnectorResult(outcome=home_outcome, failed=True)
        result = ConnectorResult(outcome="verified" if proofs else "no_proof" if absent else "inconclusive", touched={domain["asset_id"]})
        self._record(context, domain, host, proofs, absent, result)
        if listed is not None:
            self._accounts(context, domain, listed if isinstance(listed, list) else [], result)
        regrade(context.store, context.institution_id, sorted(result.touched))
        return result

    async def _homepage(self, context: ConnectorContext, host: str, token: str) -> tuple[str | None, str]:
        """("tag" | "none" | "unhealthy", or None when the page gave no usable answer; the fetch outcome)."""

        url = f"https://{host}/"
        retrieval, before = await self._retrieve(context, url, token, accept=HTML_TYPES)
        if _bare(urlparse(retrieval.url).hostname) != _bare(host):
            # The domain sends visitors to another host: a tag there proves nothing about this one.
            self._remember(context, url, token, "none")
            return "none", "redirected"
        if retrieval.outcome == "not_modified":
            # Unchanged since the last clean reading under this token: what it said then stands.
            return (before if before in {"tag", "none"} else None), retrieval.outcome
        if not retrieval.ok:
            return None, retrieval.outcome
        page = parse_structure(retrieval.text, retrieval.url)
        if assess(page, retrieval.text).status != CLEAN:
            return "unhealthy", "unhealthy"
        state = "tag" if _matches(page.meta.get(META_NAME), token) else "none"
        self._remember(context, url, token, state, retrieval)
        return state, retrieval.outcome

    async def _dns(self, host: str, token: str) -> tuple[str, bool]:
        if self.dns is None:
            return "unchecked", False
        outcome, records = await resolve(self.dns, host, "TXT")
        return outcome, outcome == "ok" and any(_matches(record.removeprefix(f"{META_NAME}="), token) for record in records if record.startswith(f"{META_NAME}="))

    async def _well_known(self, context: ConnectorContext, domain: Mapping[str, Any], host: str, token: str) -> list[str] | str | None:
        """The accounts a verified file lists; "absent" when there is no file or no valid token in it; None when it could not be read."""

        url = f"https://{host}{WELL_KNOWN_PATH}"
        retrieval, before = await self._retrieve(context, url, token, accept=frozenset({"json", "text"}), max_bytes=200_000)
        if _bare(urlparse(retrieval.url).hostname) != _bare(host) or retrieval.outcome in _FILE_ABSENT:
            # Gone, or served by another host (a redirect): the domain itself does not publish it.
            self._remember(context, url, token, "absent")
            return "absent"
        if retrieval.outcome == "not_modified":
            return self._listed_before(context, domain["asset_id"], f"owner-file:{host}") if before == "verified" else "absent" if before == "absent" else None
        if not retrieval.ok:
            return None
        try:
            payload = json.loads(retrieval.body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not _matches(payload.get("verification"), token):
            self._remember(context, url, token, "absent", retrieval)
            return "absent"
        self._remember(context, url, token, "verified", retrieval)
        accounts = payload.get("accounts")
        return [str(item) for item in accounts if isinstance(item, str)][:MAX_LISTED_ACCOUNTS] if isinstance(accounts, list) else []

    @staticmethod
    async def _retrieve(context: ConnectorContext, url: str, token: str, **options: Any) -> tuple[Retrieval, str | None]:
        """Fetch ``url`` with the validators of the last owner check that read it under this token (and what it found then).

        They are kept apart from the harvester's and keyed by the token too,
        so a 304 means "unchanged since this check last read it", and a
        rotated token always gets a full read.
        """

        state = context.store.fetch_state(context.institution_id, f"owner:{token}:{url}") or {}
        retrieval = await context.fetcher.retrieve(url, etag=state.get("etag"), last_modified=state.get("last_modified"), **options)  # type: ignore[union-attr]
        return retrieval, state.get("outcome")

    @staticmethod
    def _remember(context: ConnectorContext, url: str, token: str, found: str, retrieval: Retrieval | None = None) -> None:
        """What ``url`` said under this token, with its validators when the answer was the page itself (none otherwise)."""

        context.store.record_fetch(context.institution_id, f"owner:{token}:{url}", outcome=found, etag=retrieval.etag if retrieval else None, last_modified=retrieval.last_modified if retrieval else None, content_sha256=None)

    @staticmethod
    def _listed_before(context: ConnectorContext, domain_asset_id: str, channel: str) -> list[str]:
        """The accounts an unchanged file lists: the owner's own latest word on each (a withdrawal because the domain was lost is not the owner's)."""

        latest: dict[str, str] = {}
        for row in context.store.links_from(context.institution_id, domain_asset_id, kind="owner_claim"):
            if row["channel"] == channel and row["observed_via"] == "owner":
                latest[row["asset_id"]] = row["polarity"]
        return [asset["url"] for asset in context.store.get_assets(context.institution_id, [asset_id for asset_id, polarity in latest.items() if polarity == "supports"]).values()]

    @staticmethod
    def _latest_owner_claims(context: ConnectorContext, asset_ids: list[str], channel: str) -> dict[str, str]:
        latest: dict[str, str] = {}
        for asset_id, rows in context.store.evidence_for(context.institution_id, asset_ids).items():
            for row in rows:
                if row["kind"] == "owner_claim" and row["channel"] == channel:
                    latest[asset_id] = row["polarity"]
        return latest

    def _record(self, context: ConnectorContext, domain: Mapping[str, Any], host: str, proofs: list[str], absent: bool, result: ConnectorResult) -> None:
        asset_id, channel, url = domain["asset_id"], f"owner:{host}", f"https://{host}/"
        if proofs:
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", detail=f"domain verified by {', '.join(proofs)}", source_url=url, channel=channel, observed_via="owner", run_id=context.run_id)
        elif absent and self._latest_owner_claims(context, [asset_id], channel).get(asset_id) == "supports":
            # The token is gone: the owner's confirmation is withdrawn, and with it every account the domain's file named.
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", polarity="refutes", detail="verification token no longer published", source_url=url, channel=channel, observed_via="owner", run_id=context.run_id)
            result.touched.update(withdraw_owner_listing(context.store, context.institution_id, asset_id, detail="the domain's ownership proof was withdrawn", observed_via="owner", source_url=url, run_id=context.run_id))

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
            existing = context.store.find_asset(context.institution_id, ref.key)
            if existing is not None and not claimable(context.store, context.institution_id, existing["entity_id"], domain["entity_id"]):
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=domain["entity_id"], relation="official", note=f"listed by the owner on {host}")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="owner_claim", detail=f"listed in {WELL_KNOWN_PATH}", source_url=source_url, source_asset_id=domain["asset_id"], channel=channel, observed_via="owner", run_id=context.run_id)
            named.add(asset_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        # An account the owner listed before and has taken off the list (or deleted the file, or broken its token) is withdrawn.
        result.touched.update(withdraw_owner_listing(context.store, context.institution_id, domain["asset_id"], keep=named, detail=f"no longer listed in {WELL_KNOWN_PATH}", observed_via="owner", source_url=source_url, run_id=context.run_id))


def claimable(store: Any, institution_id: str, entity_id: str | None, owner_entity_id: str | None) -> bool:
    """Whether the owner of ``owner_entity_id`` may confirm an account the map placed with ``entity_id``.

    An account not yet placed, or placed with the owner's entity or a unit
    under it, may be; one placed with another entity, a look-alike or a
    person's role may not: a domain's file speaks for its own institution
    only, and a look-alike must never become official through it.
    """

    if not entity_id:
        return True
    current: str | None = entity_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        entity = store.get_entity(institution_id, current)
        if entity is None or (current == entity_id and entity["kind"] in {"lookalike", "role"}):
            return False
        if current == owner_entity_id:
            return True
        current = entity.get("parent_id")
    return False


def nominated_by_institution(store: Any, institution_id: str, asset_id: str) -> bool:
    """A domain the institution configured or a reviewer confirmed (an earlier owner proof counts too)."""

    return any(
        row["polarity"] == "supports" and row["kind"] in {"configured_domain", "reviewer_confirm", "owner_claim"}
        for row in store.evidence_for(institution_id, [asset_id])[asset_id]
    )


def owned_domains(store: Any, institution_id: str) -> list[dict[str, Any]]:
    return [domain for domain in store.iter_assets(institution_id, kind="domain", relation="official") if nominated_by_institution(store, institution_id, domain["asset_id"])]


__all__ = ["META_NAME", "UNHEALTHY_STATUSES", "OwnerClaimsConnector", "WELL_KNOWN_PATH", "claimable", "instructions", "nominated_by_institution", "owned_domains", "verification_token"]
