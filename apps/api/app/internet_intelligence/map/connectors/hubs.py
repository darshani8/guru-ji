"""Pages that list other accounts: link hubs (Linktree and the like) and directories.

A link hub passes on at most one grade less than its own: a Linktree the
official site links (A) makes what it lists B. A directory is judged by
who publishes it: a regulator's listing of an institution's website (AICTE,
NIRF, UGC, NAAC, the affiliating university) anchors that domain; any other
listing is community data, one channel among others.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..assets import ACCOUNT, DOMAIN, GROUP, asset_ref
from ..integrity import CLEAN, assess
from ..pipeline import anchor_grade, lose_anchor
from ..structure import BODY, HIDDEN, parse_structure
from .base import ConnectorContext, ConnectorResult, Lead
from .common import FAILED_OUTCOMES, match_entity, names_entity

# host suffix -> the authority that publishes it
AUTHORITIES: dict[str, str] = {
    "aicte-india.org": "aicte", "nirfindia.org": "nirf", "ugc.gov.in": "ugc", "ugc.ac.in": "ugc", "naac.gov.in": "naac", "vtu.ac.in": "vtu", "rguhs.ac.in": "rguhs",
    "nmc.org.in": "nmc", "dte.kar.nic.in": "dte-karnataka", "kea.kar.nic.in": "kea", "cbse.gov.in": "cbse", "kseab.karnataka.gov.in": "kseab", "education.gov.in": "moe",
}


def authority_of(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for suffix, name in AUTHORITIES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


@dataclass(slots=True)
class LinkHubConnector:
    active: bool = False
    name: str = "link_hub"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "B"
    default_interval: int = 14 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        return [
            Lead(self.name, hub["asset_id"], entity_id=hub["entity_id"], asset_id=hub["asset_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval)
            for hub in context.store.iter_assets(context.institution_id, platform="linktree", kind="account")
            if hub["asset_id"] not in existing and hub["grade"] != "D"
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        hub = context.store.get_asset(context.institution_id, str(source["target"]))
        if hub is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        retrieval = await context.fetcher.retrieve(hub["url"])
        if not retrieval.ok:
            if retrieval.outcome in {"not_found", "gone"}:
                # A deleted hub no longer vouches for what it listed.
                lose_anchor(context.store, context.institution_id, hub["asset_id"], reason=retrieval.outcome, run_id=context.run_id)
            return ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in FAILED_OUTCOMES)
        structure = parse_structure(retrieval.text, retrieval.url)
        if assess(structure, retrieval.text).status != CLEAN:
            lose_anchor(context.store, context.institution_id, hub["asset_id"], reason="unhealthy", run_id=context.run_id)
            return ConnectorResult(outcome="unhealthy")
        hub_grade = anchor_grade(context.store, context.institution_id, hub["asset_id"])
        hub_grade = hub_grade if hub_grade in {"O", "A", "B"} else "C"
        result = ConnectorResult(outcome="ok", touched={hub["asset_id"]})
        hub_host = (urlparse(retrieval.url).hostname or "").lower()
        hops = int(source.get("hops") or 0) + 1
        for link in structure.links:
            if link.position == HIDDEN or (urlparse(link.href).hostname or "").lower() == hub_host:
                continue
            try:
                ref = asset_ref(link.href)
            except ValueError:
                continue
            if ref.platform == "website":
                if ref.kind == DOMAIN and hops <= context.max_hops:
                    result.leads.append(Lead("lead_page", ref.url, entity_id=hub["entity_id"], hops=hops))
                continue
            if ref.kind not in {ACCOUNT, GROUP} or context.store.is_suppressed(context.institution_id, ref.key):
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=hub["entity_id"], relation=hub["relation"] if hub["relation"] == "official" else "unknown", note=f"listed on {hub['handle']}")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="hub_link", detail=f"{hub_grade}:hub", source_url=retrieval.url, source_asset_id=hub["asset_id"], channel=f"hub:{hub['handle']}", observed_via="live", run_id=context.run_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        return result


@dataclass(slots=True)
class DirectoryConnector:
    """Read a directory page a manager added as a source (target: its URL).

    Only links in the page body count (a directory's own header and footer
    are its own accounts), and only links whose address or text names the
    entity: "bgscet.ac.in" beside a list of a hundred colleges says which
    college it belongs to; a page-wide mention does not.
    """

    active: bool = False
    name: str = "directory"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "A"
    default_interval: int = 30 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        url = str(source["target"])
        retrieval = await context.fetcher.retrieve(url)
        if not retrieval.ok:
            return ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in FAILED_OUTCOMES, prune=retrieval.outcome == "snippet_only" or (retrieval.outcome in {"not_found", "gone"} and source.get("origin") == "lead"))
        structure = parse_structure(retrieval.text, retrieval.url)
        if assess(structure, retrieval.text).status != CLEAN:
            return ConnectorResult(outcome="unhealthy")
        authority = authority_of(retrieval.url)
        page_host = (urlparse(retrieval.url).hostname or "").lower()
        # A page about one institution (its profile on a directory) may list
        # its accounts without naming it in each link.
        # Only a page whose own title names the entity counts as being about
        # it; a listing that merely includes the name (with a hundred others)
        # must not hand every link on it to that entity.
        about = match_entity(context, url=retrieval.url, title=structure.title, text=structure.text[:3000])
        about = about if about is not None and about.score >= 0.8 and names_entity(about.entity, handle="", title=structure.title) else None
        entities = [entity for entity in context.entities() if entity["kind"] != "lookalike"]
        result = ConnectorResult(outcome="ok")
        for link in structure.links:
            if link.position != BODY or (urlparse(link.href).hostname or "").lower() == page_host:
                continue
            try:
                ref = asset_ref(link.href)
            except ValueError:
                continue
            if ref.kind not in {ACCOUNT, GROUP, DOMAIN} or context.store.is_suppressed(context.institution_id, ref.key):
                continue
            entity = next((item for item in entities if names_entity(item, handle=ref.handle, title=link.text)), None)
            if entity is None and about is not None and ref.kind in {ACCOUNT, GROUP}:
                entity = about.entity
            if entity is None:
                continue
            record = "directory_record" if authority else "community_record"
            detail = f"authority:{authority}" if authority else f"listing:{page_host}"
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=entity["entity_id"], relation="official" if authority else "unknown", note=f"listed on {page_host}")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind=record, detail=detail, source_url=retrieval.url, channel=f"directory:{authority or page_host}", observed_via="live", run_id=context.run_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        return result


__all__ = ["AUTHORITIES", "DirectoryConnector", "LinkHubConnector", "authority_of"]
