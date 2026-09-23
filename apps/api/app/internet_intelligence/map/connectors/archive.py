"""The Wayback Machine: what an official site said before it died, lapsed or was taken over.

For an official domain that is now dead, parked, hijacked or compromised,
the connector looks up recent archived captures of its homepage, takes the
newest one that is itself healthy, and records the identity links it and
the archived contact and about pages show as ``official_link`` evidence
observed in an archive. The grader turns that into A-arch ("was official
then"), never A.

For a domain the institution, a reviewer or a regulator named, it also asks
the archive once a month which hosts under that domain it has ever
captured (one CDX query, at most 500 URLs): hosts the map does not know
yet (an old admissions portal, a department site) become leads, and a
live one is then graded like any subdomain.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..assets import ACCOUNT, GROUP, asset_ref
from ..grading import grade
from ..harvest import OfficialSiteHarvester, identity_links, vouchable
from ..integrity import CLEAN, assess
from ..pipeline import nominated
from ..structure import parse_structure
from .base import ConnectorContext, ConnectorResult, Lead
from .common import ApiClient

CDX = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{timestamp}id_/{url}"
_FAILING = frozenset({"dead", "parked", "hijacked", "compromised", "redirected"})
HOSTS_SUFFIX = "|hosts"  # a source target "<domain asset id>|hosts" lists the hosts the archive knows under the domain
_ARCHIVED_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def historical_grade(context: ConnectorContext, domain: Mapping[str, Any]) -> str:
    """The grade the domain had on everything but its present health."""

    evidence = [item for item in context.store.evidence_for(context.institution_id, [domain["asset_id"]])[domain["asset_id"]] if item["kind"] not in {"integrity", "liveness"}]
    return grade(domain, evidence, now=context.now).grade


@dataclass(slots=True)
class WaybackConnector:
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "wayback"
    access_mode: str = "archive"
    budget_key: str = "wayback"
    max_grade: str = "A-arch"
    default_interval: int = 30 * 86400
    max_snapshots: int = 3
    max_pages: int = 2  # archived contact / about pages read after a healthy homepage capture
    max_hosts: int = 40

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0 if str(source.get("target", "")).endswith(HOSTS_SUFFIX) else 1.0 + self.max_snapshots + self.max_pages

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        leads = []
        for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official"):
            if domain["asset_id"] not in existing and (domain["status"] in _FAILING or domain["grade"] == "D"):
                leads.append(Lead(self.name, domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.default_interval))
            if domain["asset_id"] + HOSTS_SUFFIX not in existing and nominated(context.store, context.institution_id, domain["asset_id"]):
                leads.append(Lead(self.name, domain["asset_id"] + HOSTS_SUFFIX, entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class="explore", origin="recurring", interval_seconds=self.default_interval))
        return leads

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        target = str(source["target"])
        domain = context.store.get_asset(context.institution_id, target.removesuffix(HOSTS_SUFFIX))
        if domain is None or domain["kind"] != "domain":
            return ConnectorResult(outcome="asset_missing", prune=True)
        if target.endswith(HOSTS_SUFFIX):
            return await self._hosts(source, context, domain)
        host = domain["asset_key"].removeprefix("web:")
        listing = await self.client.request(CDX, params={"url": f"{host}/", "output": "json", "fl": "timestamp,original,statuscode,mimetype", "filter": "statuscode:200", "collapse": "timestamp:6", "limit": "-12"})
        if not listing.ok:
            return ConnectorResult(outcome=listing.outcome, failed=True)
        rows = listing.json()
        captures = [row for row in (rows[1:] if isinstance(rows, list) else []) if isinstance(row, list) and len(row) >= 2 and str(row[0]).isdigit()]
        if not captures:
            return ConnectorResult(outcome="no_captures")
        anchor = historical_grade(context, domain)
        spent = 1.0
        for timestamp, original, *_ in sorted(captures, key=lambda row: str(row[0]), reverse=True)[: self.max_snapshots]:
            snapshot = SNAPSHOT.format(timestamp=timestamp, url=original)
            spent += 1
            page = await self.client.request(snapshot, headers={"Accept": "text/html"})
            if not page.ok:
                continue
            html = page.text
            structure = parse_structure(html, str(original))
            if assess(structure, html).status != CLEAN:
                continue
            result = self._record(context, domain, structure, snapshot, timestamp, anchor, spent)
            # The site's contact and about pages as archived then (the nearest capture of each).
            for page_url in OfficialSiteHarvester._candidate_pages(structure, host)[: self.max_pages]:
                archived = SNAPSHOT.format(timestamp=timestamp, url=page_url)
                result.cost = (result.cost or 0.0) + 1
                page = await self.client.request(archived, headers={"Accept": "text/html"})
                about = parse_structure(page.text, page_url) if page.ok else None
                if about is not None and assess(about, page.text).status == CLEAN:
                    self._links(context, domain, about, archived, timestamp, anchor, result)
            return result
        return ConnectorResult(outcome="no_healthy_capture", cost=spent)

    async def _hosts(self, source: Mapping[str, Any], context: ConnectorContext, domain: Mapping[str, Any]) -> ConnectorResult:
        """Hosts under the domain the archive captured and the map does not know yet, as leads."""

        host = domain["asset_key"].removeprefix("web:")
        listing = await self.client.request(CDX, params={"url": host, "matchType": "domain", "fl": "original", "collapse": "urlkey", "filter": "statuscode:200", "limit": "500", "output": "json"})
        if not listing.ok:
            return ConnectorResult(outcome=listing.outcome, failed=True)
        rows = listing.json()
        found: list[str] = []
        for row in rows[1:] if isinstance(rows, list) else []:
            name = (urlparse(str(row[0] if isinstance(row, list) and row else "")).hostname or "").lower().removeprefix("www.")
            if name != host and name.endswith("." + host) and _ARCHIVED_HOST.match(name) and name not in found and context.store.find_asset(context.institution_id, f"web:{name}") is None:
                found.append(name)
        result = ConnectorResult(outcome="ok", cost=1.0)
        hops = int(source.get("hops") or 0) + 1
        if hops <= context.max_hops:
            result.leads.extend(Lead("lead_page", f"https://{name}/", entity_id=domain["entity_id"], hops=hops) for name in found[: self.max_hosts])
        result.notes.append(f"{len(found)} archived hosts not yet mapped")
        return result

    def _record(self, context: ConnectorContext, domain: Mapping[str, Any], structure: Any, snapshot: str, timestamp: str, anchor: str, spent: float) -> ConnectorResult:
        result = ConnectorResult(outcome="ok", cost=spent, touched={domain["asset_id"]})
        context.store.add_evidence(context.institution_id, asset_id=domain["asset_id"], kind="archive_capture", detail=f"healthy capture {timestamp}", source_url=snapshot, channel="archive", observed_via="archive", run_id=context.run_id)
        return self._links(context, domain, structure, snapshot, timestamp, anchor, result)

    def _links(self, context: ConnectorContext, domain: Mapping[str, Any], structure: Any, snapshot: str, timestamp: str, anchor: str, result: ConnectorResult) -> ConnectorResult:
        entity = context.store.get_entity(context.institution_id, domain["entity_id"]) if domain["entity_id"] else None
        for href, position, text in identity_links(structure, str(structure.url)):
            try:
                ref = asset_ref(href)
            except ValueError:
                continue
            if ref.kind not in {ACCOUNT, GROUP} or ref.platform == "website" or context.store.is_suppressed(context.institution_id, ref.key) or not vouchable(entity, ref, position, text):
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=domain["entity_id"], relation="official", note=f"linked from {domain['handle']} (archived {timestamp[:8]})")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="official_link", detail=f"{anchor}:{position}", source_url=snapshot, source_asset_id=domain["asset_id"], channel=f"archive:{domain['handle']}", observed_via="archive", run_id=context.run_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        return result


__all__ = ["WaybackConnector", "historical_grade"]
