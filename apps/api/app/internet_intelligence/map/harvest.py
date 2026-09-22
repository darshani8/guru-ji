"""Harvest official websites for the accounts they declare.

The manual sweep found its strongest evidence in the footers, headers and
contact pages of the institutions' own sites. The harvester fetches a
domain's homepage and a few contact / about pages on the same host, checks
each page's health, and records every identity link as ``official_link``
evidence whose strength is the domain's own grade: a footer link on an A
domain is A, on a B domain B. Hidden links never count, pages on other
hosts or subdomains never anchor, and an unhealthy page vouches for nothing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from ..fetch import PublicPageFetcher, Retrieval
from .assets import ACCOUNT, GROUP, asset_ref
from .integrity import CLEAN, IntegrityReport, assess
from .pipeline import anchor_grade, regrade
from .store import GRADE_RANK, MapStore
from .structure import BODY, HEAD_LINK, HIDDEN, IDENTITY_POSITIONS, PageStructure, parse_structure

_CONTACT = re.compile(r"contact|about|connect|reach[-_ ]?us|follow|social|get[-_ ]in[-_ ]touch", re.IGNORECASE)
_GUESSES = ("/contact", "/contact-us", "/about", "/about-us")
ANCHORING_GRADES = frozenset({"O", "A", "B"})


@dataclass(slots=True)
class HarvestResult:
    domain: str
    pages: list[dict[str, str]] = field(default_factory=list)
    integrity: str = "unknown"
    accounts: list[str] = field(default_factory=list)
    new_assets: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    leads: list[str] = field(default_factory=list)
    evidence: int = 0
    anchor: str = "C"
    incidents: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain, "pages": self.pages, "integrity": self.integrity, "accounts": self.accounts, "new_assets": self.new_assets, "feeds": self.feeds,
            "leads": self.leads[:50], "evidence": self.evidence, "anchor": self.anchor, "incidents": self.incidents,
        }


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


@dataclass(slots=True)
class OfficialSiteHarvester:
    fetcher: PublicPageFetcher
    store: MapStore
    max_pages: int = 5
    conditional: bool = False  # use the shared fetch cache's validators (the scheduled engine sets this)

    async def harvest(self, institution_id: str, domain_asset_id: str, *, run_id: str | None = None) -> HarvestResult:
        asset = self.store.get_asset(institution_id, domain_asset_id)
        if asset is None or asset["kind"] != "domain":
            raise ValueError("harvest needs a domain asset")
        host = asset["asset_key"].removeprefix("web:")
        result = HarvestResult(domain=host)
        # Grade the domain from its evidence first: the links it vouches for inherit that grade.
        regrade(self.store, institution_id, [domain_asset_id])
        homepage = await self._retrieve(asset["url"])
        self._liveness(institution_id, domain_asset_id, homepage, run_id)
        result.pages.append({"url": homepage.url, "outcome": homepage.outcome})
        if homepage.outcome == "not_modified":
            # Unchanged since the last fetch: the links it gave then still stand.
            result.integrity = "unchanged"
            touched = self._reconfirm(institution_id, domain_asset_id, homepage.url, run_id, result)
            regrade(self.store, institution_id, [domain_asset_id, *touched])
            return result
        if not homepage.ok:
            regrade(self.store, institution_id, [domain_asset_id])
            return result
        if _host(homepage.url) != host:
            # The domain now redirects elsewhere: it cannot vouch for anything, and the move is a finding.
            self.store.add_evidence(institution_id, asset_id=domain_asset_id, kind="integrity", polarity="refutes", detail=f"redirects_offsite:{_host(homepage.url)}", source_url=homepage.url, channel=f"site:{host}", run_id=run_id)
            result.integrity = "redirects_offsite"
            result.evidence += 1
            regrade(self.store, institution_id, [domain_asset_id])
            return result
        home = parse_structure(homepage.text, homepage.url)
        report = assess(home, homepage.text)
        self._integrity(institution_id, domain_asset_id, report, homepage.url, host, run_id, result)
        pages: list[tuple[str, PageStructure, str]] = [(homepage.url, home, homepage.text)]
        if report.status == CLEAN:
            for url in self._candidate_pages(home, host):
                if len(pages) >= self.max_pages:
                    break
                retrieval = await self._retrieve(url, conditional=False)
                result.pages.append({"url": retrieval.url, "outcome": retrieval.outcome})
                if retrieval.ok and _host(retrieval.url) == host:
                    structure = parse_structure(retrieval.text, retrieval.url)
                    page_report = assess(structure, retrieval.text)
                    if page_report.status != CLEAN:
                        self._integrity(institution_id, domain_asset_id, page_report, retrieval.url, host, run_id, result)
                        break
                    pages.append((retrieval.url, structure, retrieval.text))
        regrade(self.store, institution_id, [domain_asset_id])
        anchor = anchor_grade(self.store, institution_id, domain_asset_id)
        result.anchor = anchor
        if result.integrity != CLEAN or anchor not in ANCHORING_GRADES:
            result.feeds = list(dict.fromkeys(feed for _, structure, _ in pages for feed in structure.feeds))
            return result
        touched: set[str] = set()
        seen_on_page: dict[str, set[str]] = {}
        for page_url, structure, _ in pages:
            result.feeds.extend(feed for feed in structure.feeds if feed not in result.feeds)
            # Any visible link to another website is a lead for discovery (a
            # sister institution, a portal); only identity links vouch.
            for link in structure.links:
                if link.position != HIDDEN and len(result.leads) < 100 and link.href.startswith(("http://", "https://")) and _host(link.href) not in {host, ""}:
                    try:
                        if asset_ref(link.href).platform == "website" and link.href not in result.leads:
                            result.leads.append(link.href)
                    except ValueError:
                        continue
            for href, position in self._identity_links(structure, page_url):
                try:
                    ref = asset_ref(href)
                except ValueError:
                    continue
                if ref.platform == "website":
                    continue
                if ref.kind not in {ACCOUNT, GROUP} or self.store.is_suppressed(institution_id, ref.key):
                    continue
                seen_on_page.setdefault(page_url, set()).add(ref.key)
                target_id, created = self.store.upsert_asset(institution_id, ref, entity_id=asset["entity_id"], relation="official", note=f"linked from {host}")
                self.store.add_evidence(institution_id, asset_id=target_id, kind="official_link", detail=f"{anchor}:{position}", source_url=page_url, source_asset_id=domain_asset_id, channel=f"site:{host}", observed_via="live", run_id=run_id)
                result.evidence += 1
                touched.add(target_id)
                if ref.key not in result.accounts:
                    result.accounts.append(ref.key)
                if created:
                    result.new_assets.append(ref.key)
        touched |= self._record_removals(institution_id, domain_asset_id, {url for url, _, _ in pages}, seen_on_page, run_id, result)
        regrade(self.store, institution_id, sorted(touched))
        return result

    async def _retrieve(self, url: str, *, conditional: bool | None = None) -> Retrieval:
        use_cache = self.conditional if conditional is None else conditional
        state = self.store.fetch_state(url) if use_cache else None
        retrieval = await self.fetcher.retrieve(url, etag=(state or {}).get("etag"), last_modified=(state or {}).get("last_modified"))
        if self.conditional:
            digest = hashlib.sha256(retrieval.body).hexdigest() if retrieval.ok else None
            self.store.record_fetch(url, outcome=retrieval.outcome, etag=retrieval.etag if retrieval.outcome in {"ok", "not_modified"} else None, last_modified=retrieval.last_modified if retrieval.outcome in {"ok", "not_modified"} else None, content_sha256=digest)
        return retrieval

    def _reconfirm(self, institution_id: str, domain_asset_id: str, page_url: str, run_id: str | None, result: HarvestResult) -> set[str]:
        """Repeat the latest official-link observation from an unchanged page (a 304 answer)."""

        latest: dict[str, dict[str, object]] = {}
        for item in self.store.list_evidence(institution_id, limit=20000):
            if item["kind"] == "official_link" and item["source_asset_id"] == domain_asset_id and item["source_url"] == page_url:
                latest[item["asset_id"]] = item
        touched: set[str] = set()
        for asset_id, item in latest.items():
            if item["polarity"] != "supports":
                continue
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="official_link", detail=str(item["detail"]), source_url=page_url, source_asset_id=domain_asset_id, channel=str(item["channel"]), observed_via="live", run_id=run_id)
            result.evidence += 1
            touched.add(asset_id)
        return touched

    def _record_removals(self, institution_id: str, domain_asset_id: str, fetched: set[str], seen_on_page: dict[str, set[str]], run_id: str | None, result: HarvestResult) -> set[str]:
        """A page fetched cleanly that no longer links an account it used to link refutes that link."""

        removed: set[str] = set()
        linked_before: dict[tuple[str, str], str] = {}
        for item in self.store.list_evidence(institution_id, limit=20000):
            if item["kind"] == "official_link" and item["source_asset_id"] == domain_asset_id and item["source_url"] in fetched:
                linked_before[(item["asset_id"], item["source_url"])] = item["polarity"]
        for (asset_id, page_url), polarity in linked_before.items():
            asset = self.store.get_asset(institution_id, asset_id)
            if asset is None or polarity != "supports" or asset["asset_key"] in seen_on_page.get(page_url, set()):
                continue
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="official_link", polarity="refutes", detail=f"removed:{page_url}"[:500], source_url=page_url, source_asset_id=domain_asset_id, channel=f"site:{result.domain}", observed_via="live", run_id=run_id)
            result.evidence += 1
            removed.add(asset_id)
        return removed

    def _liveness(self, institution_id: str, asset_id: str, retrieval: Retrieval, run_id: str | None) -> None:
        if retrieval.outcome in {"ok", "not_modified"}:
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="liveness", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=run_id)
        elif retrieval.outcome in {"not_found", "gone", "blocked", "login_wall", "robots", "server_error", "timeout", "not_public"}:
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="liveness", polarity="refutes", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=run_id)

    def _integrity(self, institution_id: str, asset_id: str, report: IntegrityReport, page_url: str, host: str, run_id: str | None, result: HarvestResult) -> None:
        clean = report.status == CLEAN
        self.store.add_evidence(
            institution_id, asset_id=asset_id, kind="integrity", polarity="supports" if clean else "refutes", detail=f"{report.status}:{';'.join(report.signals)}"[:500],
            source_url=page_url, channel=f"site:{host}", observed_via="live", run_id=run_id,
        )
        result.evidence += 1
        if result.integrity in {"unknown", CLEAN}:
            result.integrity = report.status
        if not clean:
            result.incidents.append({"kind": f"site_{report.status}", "target": host, "page": page_url, "signals": report.signals, "examples": report.spam_links[:5]})

    @staticmethod
    def _candidate_pages(home: PageStructure, host: str) -> list[str]:
        found: list[str] = []
        for link in home.links:
            if link.position == HIDDEN or _host(link.href) != host:
                continue
            if _CONTACT.search(urlparse(link.href).path) or _CONTACT.search(link.text):
                clean = link.href.split("#", 1)[0]
                if clean.rstrip("/") != home.url.rstrip("/") and clean not in found:
                    found.append(clean)
        if not found:
            found = [urljoin(home.url, guess) for guess in _GUESSES[:2]]
        return found

    @staticmethod
    def _identity_links(structure: PageStructure, page_url: str) -> list[tuple[str, str]]:
        """(href, position) of links that declare identity on this page."""

        contact_page = bool(_CONTACT.search(urlparse(page_url).path))
        out: list[tuple[str, str]] = []
        for link in structure.links:
            if link.position == HIDDEN:
                continue
            if link.position in IDENTITY_POSITIONS or link.position == HEAD_LINK or (contact_page and link.position == BODY):
                out.append((link.href, "rel_me" if link.position == HEAD_LINK else ("contact_page" if link.position == BODY else link.position)))
        out.extend((href, "same_as") for href in structure.same_as)
        return list(dict.fromkeys(out))


def best(grades: list[str]) -> str:
    return max(grades, key=lambda value: GRADE_RANK.get(value, 1)) if grades else "unrated"


__all__ = ["ANCHORING_GRADES", "HarvestResult", "OfficialSiteHarvester"]
