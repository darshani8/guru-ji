"""Connectors over a licensed search index: platform-by-platform account discovery and the spam probe.

Search results are snippets, never fetched pages: an account found this way
is a C-grade candidate from one channel until something stronger (an
official link, a second independent channel, a reviewer) agrees. A result's
title is kept only with people's names taken out (the mapped entities' own
names stay): the map records where an account was seen, not who posted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ...redaction import names_of, strip_person_names
from ...search import IntelligenceSearchUnavailable
from ..assets import ACCOUNT, DOMAIN, GROUP, asset_ref, platform_of
from ..cache import calls_made, calls_so_far
from ..integrity import spam_terms
from ..store import GRADE_RANK
from .base import ConnectorContext, ConnectorResult, Lead
from .common import match_entity, names_entity, person_shaped

# The platforms searched one at a time for each mapped entity.
PLATFORM_DOMAINS: tuple[str, ...] = ("instagram.com", "facebook.com", "youtube.com", "linkedin.com", "x.com", "threads.net", "reddit.com", "t.me", "github.com", "linktr.ee")
# What a name is searched with, in turn: the institution's own accounts are
# often named for a unit or an activity ("BGSCET alumni", "BGSCET NSS").
SEARCH_TOPICS: tuple[str, ...] = ("", "official", "alumni", "students", "department", "club", "fest", "placements")
# Three kinds of paid search (the plan's 60/20/20): name x topic rotation per
# platform, re-verifying known accounts, and open-web searches for new leads.
VERIFY_PREFIX = "verify|"
OPEN_WEB = "*"
_SPAM_QUERY = "casino OR slot OR gacor OR togel OR judi OR viagra OR replica OR betting"


# platform -> the domain its accounts are searched under
_DOMAIN_OF: dict[str, str] = {platform_of(f"https://{domain}/"): domain for domain in PLATFORM_DOMAINS}


def _entities(context: ConnectorContext, limit: int | None) -> list[dict[str, Any]]:
    """Every active entity, the institution's own first (a cap here left whole groups unsearched)."""

    from ..learning import ranked_entities  # learning imports this module

    entities = [entity for entity in ranked_entities(context.store, context.institution_id) if entity.get("status", "active") == "active"]
    return entities if limit is None else entities[:limit]


@dataclass(slots=True)
class SearchConnector:
    """Search each platform for each mapped entity (target ``<entity_id>|<platform domain>``)."""

    active: bool = False
    name: str = "search"
    access_mode: str = "search_index"
    budget_key: str = "search"
    max_grade: str = "C"
    default_interval: int = 14 * 86400
    # None: a source for every entity; the daily search budget, not a cap, bounds what the rotation costs.
    max_entities: int | None = None
    results_per_query: int = 10
    # Known accounts re-verified in the index at a time (the stalest first).
    max_verify: int = 50
    verify_interval: int = 30 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        if context.search is None:
            return []
        existing = context.store.source_targets(context.institution_id, self.name)
        leads = []
        for entity in _entities(context, self.max_entities):
            for domain in PLATFORM_DOMAINS:
                target = f"{entity['entity_id']}|{domain}"
                if target not in existing:
                    leads.append(Lead(self.name, target, entity_id=entity["entity_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval))
            # New leads: the open web, where the entity's other sites and pages turn up.
            target = f"{entity['entity_id']}|{OPEN_WEB}"
            if target not in existing:
                leads.append(Lead(self.name, target, entity_id=entity["entity_id"], hops=0, work_class="explore", origin="recurring", interval_seconds=self.default_interval))
        # Re-verification: the known accounts the map has gone longest without seeing again.
        stale = sorted(
            (asset for asset in context.store.iter_assets(context.institution_id, kind="account") if asset["grade"] in {"B", "C"} and asset["platform"] in _DOMAIN_OF and f"{VERIFY_PREFIX}{asset['asset_id']}" not in existing),
            key=lambda asset: (str(asset.get("last_verified_at") or ""), str(asset.get("first_seen_at") or "")),
        )
        leads.extend(
            Lead(self.name, f"{VERIFY_PREFIX}{asset['asset_id']}", entity_id=asset["entity_id"], asset_id=asset["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.verify_interval)
            for asset in stale[: self.max_verify]
        )
        return leads

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.search is None:
            return ConnectorResult(outcome="search_disabled", failed=True, cost=0.0)
        if str(source["target"]).startswith(VERIFY_PREFIX):
            return await self._verify(source, context)
        entity_id, _, domain = str(source["target"]).partition("|")
        entity = context.store.get_entity(context.institution_id, entity_id)
        if entity is None or not domain:
            return ConnectorResult(outcome="entity_missing", prune=True, cost=0.0)
        # One of the entity's names per run, in turn, and on a platform one topic
        # per round of names (keyed off how often this source has run): an
        # account listed under "BGSCET", a Kannada name or "BGSCET alumni" is
        # never found by searching the full English name alone.
        names = list(dict.fromkeys(str(name).strip() for name in [entity["name"], *(entity.get("names") or [])] if str(name).strip()))
        location = (entity.get("locations") or [""])[0]
        runs = int(source.get("runs") or 0)
        topic = SEARCH_TOPICS[(runs // len(names)) % len(SEARCH_TOPICS)] if domain != OPEN_WEB else ""
        query = " ".join(part for part in (f'"{names[runs % len(names)]}"', topic, location) if part)[:300]
        provider = getattr(context.search, "provider_name", "search")
        before = calls_so_far(context.search)
        try:
            hits = await context.search.search(query, max_results=self.results_per_query, include_domains=[domain] if domain != OPEN_WEB else ())
        except IntelligenceSearchUnavailable as exc:
            return ConnectorResult(outcome="search_unavailable", failed=True, notes=[str(exc)[:200]])
        # The cost is the provider calls really made: an answer from the shared cache costs 0.
        result = ConnectorResult(outcome="ok", cost=calls_made(context.search, before))
        anchored = self._anchored_accounts(context)
        keep = names_of(context.entities())
        for hit in hits:
            try:
                ref = asset_ref(hit.url)
            except ValueError:
                continue
            if ref.platform == "website":
                found = match_entity(context, url=hit.url, title=hit.title, text=hit.snippet)
                if found is not None and found.score >= 0.8 and ref.kind in {DOMAIN, "page"}:
                    host = urlparse(hit.url).hostname or ""
                    result.leads.append(Lead("lead_page", f"https://{host}/", entity_id=found.entity["entity_id"], hops=1))
                continue
            if ref.kind not in {ACCOUNT, GROUP} or context.store.is_suppressed(context.institution_id, ref.key):
                continue
            close: list[Any] = []
            found = match_entity(context, url=hit.url, title=hit.title, text=hit.snippet, close=close)
            if found is None and close and names_entity(close[0].entity, handle=ref.handle, title=hit.title) and not person_shaped(ref.key):
                # An account that names one of ours about as strongly as a look-alike: a person decides.
                asset_id, _ = context.store.upsert_asset(context.institution_id, ref, entity_id=close[0].entity["entity_id"], relation="unknown", note="a close call between one of ours and a look-alike")
                result.review.append({
                    "kind": "candidate_account", "asset_id": asset_id, "entity_id": close[0].entity["entity_id"], "url": ref.url,
                    "title": f"Close call: {ref.handle} names {close[0].entity['name'][:80]} about as strongly as a look-alike",
                    "detail": f"{close[0].score:.2f} against a look-alike's {close[0].rival:.2f}; confirm it if it is ours, mark it a look-alike if not.",
                })
                continue
            if found is None or not names_entity(found.entity, handle=ref.handle, title=hit.title):
                continue
            if person_shaped(ref.key) and not names_entity(found.entity, handle=ref.handle, title=""):
                # A personal-profile URL (LinkedIn /in/, a phone number) is kept only
                # when its own handle is the institution's, not just its display name.
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=found.entity["entity_id"], relation="unknown", note=f"found by searching {domain if domain != OPEN_WEB else 'the open web'}")
            title = strip_person_names(hit.title, keep=keep)[:120]
            context.store.add_evidence(
                context.institution_id, asset_id=asset_id, kind="search_snippet", detail=f"{provider}: {title}", source_url=hit.url, channel=f"search:{provider}", observed_via="index", run_id=context.run_id,
            )
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
            claimed_official = "official" in hit.title.lower()
            rival = anchored.get((found.entity["entity_id"], ref.platform))
            if claimed_official and rival and ref.key not in rival:
                result.review.append({
                    "kind": "impersonation_candidate", "asset_id": asset_id, "title": f"{ref.handle} calls itself official on {ref.platform}",
                    "detail": f"{found.entity['name']} already has an anchored {ref.platform} account ({', '.join(sorted(rival))[:120]}); this one says '{title}'", "url": hit.url,
                })
        return result

    async def _verify(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        """Search a known account's own handle on its platform: is the index still showing it?

        Found again, it gets a fresh index observation; not found proves
        nothing (an index is not the platform), so nothing is taken away.
        """

        asset = context.store.get_asset(context.institution_id, str(source["target"]).removeprefix(VERIFY_PREFIX))
        if asset is None or asset["kind"] != "account" or asset["platform"] not in _DOMAIN_OF or asset["grade"] not in {"B", "C"}:
            return ConnectorResult(outcome="asset_missing", prune=True, cost=0.0)
        term = asset["asset_key"].split(":")[-1]
        provider = getattr(context.search, "provider_name", "search")
        before = calls_so_far(context.search)
        try:
            hits = await context.search.search(f'"{term}"'[:300], max_results=self.results_per_query, include_domains=[_DOMAIN_OF[asset["platform"]]])
        except IntelligenceSearchUnavailable as exc:
            return ConnectorResult(outcome="search_unavailable", failed=True, notes=[str(exc)[:200]])
        result = ConnectorResult(outcome="not_in_index", cost=calls_made(context.search, before))
        keep = names_of(context.entities())
        for hit in hits:
            try:
                if asset_ref(hit.url).key != asset["asset_key"]:
                    continue
            except ValueError:
                continue
            title = strip_person_names(hit.title, keep=keep)[:120]
            context.store.add_evidence(
                context.institution_id, asset_id=asset["asset_id"], kind="search_snippet", detail=f"{provider}: re-verified: {title}", source_url=hit.url, channel=f"search:{provider}", observed_via="index", run_id=context.run_id,
            )
            result.outcome = "reverified"
            result.touched.add(asset["asset_id"])
            break
        return result

    @staticmethod
    def _anchored_accounts(context: ConnectorContext) -> dict[tuple[str, str], set[str]]:
        anchored: dict[tuple[str, str], set[str]] = {}
        for asset in context.store.iter_assets(context.institution_id, kind="account", relation="official"):
            if GRADE_RANK.get(asset["grade"], 1) >= GRADE_RANK["A"] and asset["entity_id"]:
                anchored.setdefault((asset["entity_id"], asset["platform"]), set()).add(asset["asset_key"])
        return anchored


@dataclass(slots=True)
class SpamProbeConnector:
    """Ask the search index whether an official domain has spam pages indexed under it.

    Injected SEO spam is often cloaked (shown to search engines, hidden from
    visitors), so a clean homepage does not prove a clean site. What the
    probe finds is reported as an incident; it does not change a grade.
    """

    active: bool = False
    name: str = "spam_probe"
    access_mode: str = "search_index"
    budget_key: str = "search"
    max_grade: str = "C"
    default_interval: int = 7 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        if context.search is None:
            return []
        existing = context.store.source_targets(context.institution_id, self.name)
        return [
            Lead(self.name, domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.default_interval)
            for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official")
            if domain["asset_id"] not in existing and domain["grade"] in {"O", "A", "B"}
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.search is None:
            return ConnectorResult(outcome="search_disabled", failed=True, cost=0.0)
        domain = context.store.get_asset(context.institution_id, str(source["target"]))
        if domain is None or domain["kind"] != "domain":
            return ConnectorResult(outcome="asset_missing", prune=True, cost=0.0)
        host = domain["asset_key"].removeprefix("web:")
        before = calls_so_far(context.search)
        try:
            hits = await context.search.search(_SPAM_QUERY, max_results=10, include_domains=[host])
        except IntelligenceSearchUnavailable as exc:
            return ConnectorResult(outcome="search_unavailable", failed=True, notes=[str(exc)[:200]])
        spam = []
        for hit in hits:
            hit_host = (urlparse(hit.url).hostname or "").lower().removeprefix("www.")
            terms = spam_terms(f"{hit.title} {hit.snippet} {urlparse(hit.url).path}")
            if (hit_host == host or hit_host.endswith("." + host)) and terms:
                spam.append((hit.url, terms))
        result = ConnectorResult(outcome="spam_found" if spam else "clean", touched={domain["asset_id"]}, cost=calls_made(context.search, before))
        if spam:
            detail = f"spam_indexed:{len(spam)}:{','.join(sorted({term for _, terms in spam for term in terms}))[:120]}"
            context.store.add_evidence(context.institution_id, asset_id=domain["asset_id"], kind="spam_indexed", polarity="refutes", detail=detail, source_url=spam[0][0], channel=f"search:{getattr(context.search, 'provider_name', 'search')}", observed_via="index", run_id=context.run_id)
            result.incidents.append({"kind": "site_spam_indexed", "target": host, "signals": [detail], "examples": [url for url, _ in spam[:5]]})
        return result


__all__ = ["OPEN_WEB", "PLATFORM_DOMAINS", "SEARCH_TOPICS", "SearchConnector", "SpamProbeConnector", "VERIFY_PREFIX"]
