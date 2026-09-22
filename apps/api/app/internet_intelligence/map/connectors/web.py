"""Connectors that read public web pages: official sites, leads found on them, and re-checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ...entity_resolution import HIGH, MEDIUM, resolve_entity
from ...profile import InstitutionProfile
from ..assets import DOMAIN, asset_ref
from ..harvest import OfficialSiteHarvester
from ..integrity import CLEAN, assess
from ..pipeline import regrade
from ..structure import IDENTITY_POSITIONS, parse_structure
from .base import ConnectorContext, ConnectorResult, Lead

_FAILURES = frozenset({"robots", "blocked", "login_wall", "not_found", "gone", "server_error", "timeout", "content_type", "too_large", "not_public", "redirect_loop", "error"})


@dataclass(slots=True)
class OfficialSiteConnector:
    """Re-harvest an official domain for the accounts it declares (target: the domain's asset id)."""

    name: str = "official_site"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "A"
    default_interval: int = 7 * 86400
    max_pages: int = 5

    def enabled(self) -> bool:
        return True

    def cost(self, source: Mapping[str, Any]) -> float:
        return float(self.max_pages)

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        domain = context.store.get_asset(context.institution_id, str(source["target"]))
        if domain is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        before = {asset["asset_key"]: asset["grade"] for asset in context.store.list_assets(context.institution_id, limit=5000)}
        harvest = await OfficialSiteHarvester(context.fetcher, context.store, max_pages=self.max_pages, conditional=True).harvest(context.institution_id, domain["asset_id"], run_id=context.run_id)
        after = {asset["asset_key"]: asset for asset in context.store.list_assets(context.institution_id, limit=5000)}
        raised = [key for key, asset in after.items() if key in before and _rank(asset["grade"]) > _rank(before[key])]
        result = ConnectorResult(outcome=harvest.pages[0]["outcome"] if harvest.pages else "error", new_assets=list(harvest.new_assets), incidents=list(harvest.incidents))
        result.failed = result.outcome in _FAILURES
        result.touched = {after[key]["asset_id"] for key in [*harvest.accounts, *raised] if key in after} | {domain["asset_id"]}
        result.yield_count = len(harvest.new_assets) + len(raised)
        hops = int(source.get("hops") or 0) + 1
        if hops <= context.max_hops:
            result.leads.extend(Lead("lead_page", url, entity_id=domain["entity_id"], hops=hops) for url in harvest.leads[:25])
        result.leads.extend(Lead("feed", url, entity_id=domain["entity_id"], hops=hops, work_class="rotation", origin="recurring", interval_seconds=6 * 3600) for url in harvest.feeds[:5])
        return result


def _rank(grade: str) -> int:
    from ..store import GRADE_RANK

    return GRADE_RANK.get(grade, 1)


def entity_profiles(context: ConnectorContext) -> tuple[list[tuple[dict[str, Any], InstitutionProfile]], list[tuple[dict[str, Any], InstitutionProfile]]]:
    """(ours, lookalikes) as resolver profiles, one per entity."""

    ours: list[tuple[dict[str, Any], InstitutionProfile]] = []
    lookalikes: list[tuple[dict[str, Any], InstitutionProfile]] = []
    for entity in context.entities():
        try:
            profile = InstitutionProfile(context.institution_id, entity["name"], (entity.get("locations") or [""])[0], aliases=entity.get("names") or [])
        except ValueError:
            continue
        (lookalikes if entity["kind"] == "lookalike" else ours).append((entity, profile))
    return ours, lookalikes


@dataclass(slots=True)
class LeadPageConnector:
    """Visit a website linked from an official page and decide whether it belongs to a mapped entity.

    Leads never become official on their own: a matching site is recorded
    as a C-grade candidate with a backlink, for the grader and the review
    queue to take further. A site that names no mapped entity (or names a
    look-alike at least as strongly) is pruned.
    """

    name: str = "lead_page"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "C"
    default_interval: int = 14 * 86400
    margin: float = 0.2

    def enabled(self) -> bool:
        return True

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        target = str(source["target"])
        try:
            host_ref = asset_ref(f"{urlparse(target).scheme or 'https'}://{urlparse(target).hostname}/")
        except ValueError:
            return ConnectorResult(outcome="invalid", prune=True)
        retrieval = await context.fetcher.retrieve(host_ref.url)
        if not retrieval.ok:
            return ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in _FAILURES, prune=retrieval.outcome in {"not_found", "gone", "robots", "not_public"})
        structure = parse_structure(retrieval.text, retrieval.url)
        if assess(structure, retrieval.text).status != CLEAN:
            return ConnectorResult(outcome="unhealthy", prune=True)
        ours, lookalikes = entity_profiles(context)
        best_entity, best_score = None, 0.0
        for entity, profile in ours:
            match = resolve_entity(profile, url=retrieval.url, title=structure.title, text=structure.text)
            if match.level in {HIGH, MEDIUM} and match.score > best_score:
                best_entity, best_score = entity, match.score
        rival = max((resolve_entity(profile, url=retrieval.url, title=structure.title, text=structure.text).score for _, profile in lookalikes), default=0.0)
        if best_entity is None or best_score < rival + self.margin:
            return ConnectorResult(outcome="no_entity_match", prune=True)
        if context.store.is_suppressed(context.institution_id, host_ref.key):
            return ConnectorResult(outcome="suppressed", prune=True)
        asset_id, created = context.store.upsert_asset(context.institution_id, host_ref, entity_id=best_entity["entity_id"], relation="unknown", note="found through a link on a mapped site")
        context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="backlink", detail=f"matches {best_entity['name'][:80]} ({best_score:.2f})", source_url=target, channel="lead", observed_via="live", run_id=context.run_id)
        result = ConnectorResult(outcome="ok", touched={asset_id}, new_assets=[host_ref.key] if created else [], yield_count=int(created))
        hops = int(source.get("hops") or 0)
        for link in structure.links:
            if link.position in IDENTITY_POSITIONS:
                try:
                    ref = asset_ref(link.href)
                except ValueError:
                    continue
                if ref.is_social and ref.kind in {"account", "group"} and not context.store.is_suppressed(context.institution_id, ref.key):
                    account_id, account_new = context.store.upsert_asset(context.institution_id, ref, entity_id=best_entity["entity_id"], relation="unknown", note=f"linked from {host_ref.handle}")
                    # The site itself is only C, so what it links is C as well.
                    context.store.add_evidence(context.institution_id, asset_id=account_id, kind="hub_link", detail=f"C:{link.position}", source_url=retrieval.url, source_asset_id=asset_id, channel=f"site:{host_ref.handle}", observed_via="live", run_id=context.run_id)
                    result.touched.add(account_id)
                    if account_new:
                        result.new_assets.append(ref.key)
                        result.yield_count += 1
        if host_ref.kind == DOMAIN and hops + 1 <= context.max_hops:
            result.notes.append("candidate site recorded; it anchors nothing until verified")
        regrade(context.store, context.institution_id, sorted(result.touched))
        return result


@dataclass(slots=True)
class RecheckConnector:
    """Re-check that a fetchable asset (a site or page) still answers; social platforms are left alone."""

    name: str = "recheck"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "C"
    default_interval: int = 14 * 86400

    def enabled(self) -> bool:
        return True

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        asset = context.store.get_asset(context.institution_id, str(source["target"]))
        if asset is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        if not context.fetcher.allowed_domain(asset["url"]):
            return ConnectorResult(outcome="snippet_only")
        state = context.store.fetch_state(asset["url"])
        retrieval = await context.fetcher.retrieve(asset["url"], etag=(state or {}).get("etag"), last_modified=(state or {}).get("last_modified"))
        context.store.record_fetch(asset["url"], outcome=retrieval.outcome, etag=retrieval.etag, last_modified=retrieval.last_modified, content_sha256=None)
        if retrieval.outcome in {"ok", "not_modified"}:
            context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=context.run_id)
        elif retrieval.outcome in _FAILURES:
            context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", polarity="refutes", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=context.run_id)
        changes = regrade(context.store, context.institution_id, [asset["asset_id"]])
        return ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in _FAILURES, touched={asset["asset_id"]}, yield_count=0, notes=[f"{change['from']}->{change['to']}" for change in changes])


__all__ = ["LeadPageConnector", "OfficialSiteConnector", "RecheckConnector", "entity_profiles"]
