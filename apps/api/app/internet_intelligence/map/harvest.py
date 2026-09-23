"""Harvest official websites for the accounts they declare.

The manual sweep found its strongest evidence in the footers, headers and
contact pages of the institutions' own sites. The harvester fetches a
domain's homepage and a few contact / about pages on the same host, checks
each page's health, and records every identity link as ``official_link``
evidence whose strength is the domain's own grade: a footer link on an A
domain is A, on a B domain B. Hidden links never count, pages on other
hosts or subdomains never anchor, and an unhealthy page vouches for nothing.
A site vouches only for its own entity and that entity's units: a link to
an account of another authority's entity (the Math's page, the Swamiji's X
account) is held at C and queued for that authority's approvers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from ..fetch import PublicPageFetcher, Retrieval
from .assets import ACCOUNT, GROUP, asset_ref
from .authority import authorities, within
from .integrity import CLEAN, IntegrityReport, assess
from .connectors.common import names_entity, person_shaped
from .pipeline import anchor_grade, lose_anchor, nominated, regrade
from .store import GRADE_RANK, MapStore
from .structure import BODY, HEAD_LINK, HIDDEN, IDENTITY_POSITIONS, PageStructure, parse_sitemap, parse_structure

_CONTACT = re.compile(r"contact|about|connect|reach[-_ ]?us|follow|social|get[-_ ]in[-_ ]touch", re.IGNORECASE)
_GUESSES = ("/contact", "/contact-us", "/about", "/about-us")
# A sitemap page named for the site's accounts ("/social-media", "/follow-us-on-instagram").
_SOCIAL_PAGE = re.compile(r"(?:^|[-_])(?:follow|social)(?:[-_.]|s?$)", re.IGNORECASE)
MAX_SITEMAP_READS = 2
MAX_SITEMAP_BYTES = 2_000_000
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
    raised: list[str] = field(default_factory=list)  # accounts whose grade went up in this harvest
    requests: int = 0  # page requests actually made (what the harvest really cost)
    held: list[str] = field(default_factory=list)  # linked accounts of another authority's entity: C, queued for its approvers

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain, "pages": self.pages, "integrity": self.integrity, "accounts": self.accounts, "new_assets": self.new_assets, "feeds": self.feeds,
            "leads": self.leads[:50], "evidence": self.evidence, "anchor": self.anchor, "incidents": self.incidents, "held": self.held,
        }


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


@dataclass(slots=True)
class OfficialSiteHarvester:
    fetcher: PublicPageFetcher
    store: MapStore
    max_pages: int = 5
    conditional: bool = False  # reuse this institution's validators from its last clean harvest (the scheduled engine sets this)

    async def harvest(self, institution_id: str, domain_asset_id: str, *, run_id: str | None = None) -> HarvestResult:
        asset = self.store.get_asset(institution_id, domain_asset_id)
        if asset is None or asset["kind"] != "domain":
            raise ValueError("harvest needs a domain asset")
        host = asset["asset_key"].removeprefix("web:")
        result = HarvestResult(domain=host)
        entity = self.store.get_entity(institution_id, asset["entity_id"]) if asset["entity_id"] else None
        # Grade the domain from its evidence first: the links it vouches for inherit that grade.
        regrade(self.store, institution_id, [domain_asset_id])
        homepage = await self._retrieve(institution_id, asset["url"], result)
        if homepage.outcome == "not_modified":
            # Unchanged since this institution's last clean harvest: the links it gave then still stand.
            touched = self._reconfirm(institution_id, domain_asset_id, homepage.url, run_id, result, home_id=asset["entity_id"])
            current = self.store.get_asset(institution_id, domain_asset_id) or asset
            if touched and current["status"] == "live":
                self._liveness(institution_id, domain_asset_id, homepage, run_id)
                result.pages.append({"url": homepage.url, "outcome": homepage.outcome})
                result.integrity = "unchanged"
                self._note_raised(regrade(self.store, institution_id, [domain_asset_id, *touched]), result)
                return result
            # Nothing of ours to stand on (or the site was unhealthy last time): read it afresh.
            homepage = await self._retrieve(institution_id, asset["url"], result, conditional=False)
        if homepage.ok and _host(homepage.url) != host:
            # Another host answered: that says nothing about this domain being live.
            self.store.add_evidence(institution_id, asset_id=domain_asset_id, kind="liveness", polarity="refutes", detail=f"redirected:{_host(homepage.url)}", source_url=homepage.url, channel="fetch", observed_via="live", run_id=run_id)
        else:
            self._liveness(institution_id, domain_asset_id, homepage, run_id)
        result.pages.append({"url": homepage.url, "outcome": homepage.outcome})
        if not homepage.ok:
            regrade(self.store, institution_id, [domain_asset_id])
            current = self.store.get_asset(institution_id, domain_asset_id) or asset
            if current["status"] == "dead":
                lose_anchor(self.store, institution_id, domain_asset_id, reason="dead", run_id=run_id)
            return result
        if _host(homepage.url) != host:
            # The domain now redirects elsewhere: it cannot vouch for anything, and the move is a finding.
            self.store.add_evidence(institution_id, asset_id=domain_asset_id, kind="integrity", polarity="refutes", detail=f"redirects_offsite:{_host(homepage.url)}", source_url=homepage.url, channel=f"site:{host}", run_id=run_id)
            result.integrity = "redirects_offsite"
            result.evidence += 1
            regrade(self.store, institution_id, [domain_asset_id])
            lose_anchor(self.store, institution_id, domain_asset_id, reason="redirected", run_id=run_id)
            return result
        home = parse_structure(homepage.text, homepage.url)
        report = assess(home, homepage.text)
        self._integrity(institution_id, domain_asset_id, report, homepage.url, host, run_id, result, homepage.body)
        if report.status in {"parked", "hijacked"}:
            lose_anchor(self.store, institution_id, domain_asset_id, reason=report.status, run_id=run_id)
        # (page URL, structure, sha256 of the body it was read from)
        pages: list[tuple[str, PageStructure, str]] = [(homepage.url, home, hashlib.sha256(homepage.body).hexdigest())]
        if report.status == CLEAN:
            # The reservation is max_pages fetches: count attempts (sitemap reads too), not successes.
            room = max(0, self.max_pages - 1)
            listed, reads = await self._sitemap_pages(home, host, result, room=room - len(self._candidate_pages(home, host, guess=False)))
            for url in self._candidate_pages(home, host, listed)[: max(0, room - reads)]:
                retrieval = await self._retrieve(institution_id, url, result, conditional=False)
                result.pages.append({"url": retrieval.url, "outcome": retrieval.outcome})
                if retrieval.ok and _host(retrieval.url) == host:
                    structure = parse_structure(retrieval.text, retrieval.url)
                    page_report = assess(structure, retrieval.text)
                    if page_report.status != CLEAN:
                        self._integrity(institution_id, domain_asset_id, page_report, retrieval.url, host, run_id, result, retrieval.body)
                        break
                    pages.append((retrieval.url, structure, hashlib.sha256(retrieval.body).hexdigest()))
        regrade(self.store, institution_id, [domain_asset_id])
        anchor = anchor_grade(self.store, institution_id, domain_asset_id)
        # Official only by inference (a subdomain, Wikidata, a directory): it never
        # anchors until the institution or a reviewer says it is theirs, but what it
        # links is recorded as a hub's links are, one grade below the site (B gives C).
        inferred = anchor in ANCHORING_GRADES and not nominated(self.store, institution_id, domain_asset_id)
        result.anchor = "C" if inferred else anchor
        if result.integrity != CLEAN or anchor not in ANCHORING_GRADES:
            result.feeds = list(dict.fromkeys(feed for _, structure, _ in pages for feed in structure.feeds))
            return result
        touched: set[str] = set()
        seen_on_page: dict[str, set[str]] = {}
        scope: _Scope | None = None
        for page_url, structure, digest in pages:
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
            for href, position, text in self._identity_links(structure, page_url):
                try:
                    ref = asset_ref(href)
                except ValueError:
                    continue
                if ref.platform == "website":
                    continue
                if ref.kind not in {ACCOUNT, GROUP} or self.store.is_suppressed(institution_id, ref.key) or not vouchable(entity, ref, position, text):
                    continue
                scope = scope or _Scope.load(self.store, institution_id, asset["entity_id"])
                other = scope.owner(self.store.find_asset(institution_id, ref.key), ref, text)
                if other is not None:
                    touched.add(self._hold(institution_id, domain_asset_id, host, ref, position, page_url, other, scope, run_id, result))
                    continue
                seen_on_page.setdefault(page_url, set()).add(ref.key)
                # An inferred site may be a vendor's (a "Powered by" ERP): what it links is a candidate, never official by that link.
                target_id, created = self.store.upsert_asset(institution_id, ref, entity_id=asset["entity_id"], relation="unknown" if inferred else "official", note=f"linked from {host}")
                self.store.add_evidence(institution_id, asset_id=target_id, kind="hub_link" if inferred else "official_link", detail=f"{anchor}:{position}", source_url=page_url, source_asset_id=domain_asset_id, channel=f"site:{host}", observed_via="live", run_id=run_id, raw_sha256=digest)
                result.evidence += 1
                touched.add(target_id)
                if ref.key not in result.accounts:
                    result.accounts.append(ref.key)
                if created:
                    result.new_assets.append(ref.key)
        touched |= self._record_removals(institution_id, domain_asset_id, {url: digest for url, _, digest in pages}, seen_on_page, run_id, result)
        self._note_raised(regrade(self.store, institution_id, sorted(touched)), result)
        # Only a clean, anchored harvest may be reused from a 304 next time (an inferred site's links are re-read).
        if self.conditional and not inferred:
            self.store.record_fetch(institution_id, asset["url"], outcome=homepage.outcome, etag=homepage.etag, last_modified=homepage.last_modified, content_sha256=hashlib.sha256(homepage.body).hexdigest())
        return result

    def _hold(
        self, institution_id: str, domain_asset_id: str, host: str, ref: Any, position: str, page_url: str, other: dict[str, Any], scope: "_Scope", run_id: str | None, result: HarvestResult,
    ) -> str:
        """Record a link to another authority's account as a C lead from this site, and queue it for that authority's approvers.

        It is neither an official link nor filed under the site's entity, and
        the evidence carries no channel, so it never counts as an independent
        confirmation either: a BGSCET footer cannot lift one of the Swamiji's
        X accounts above the other. A look-alike's name only holds the link
        back; the account is not filed under the look-alike.
        """

        lookalike = other["kind"] == "lookalike"
        entity_id = None if lookalike else str(other["entity_id"])
        chain = authorities(scope.entities.get, other["entity_id"])
        target_id, created = self.store.upsert_asset(institution_id, ref, entity_id=entity_id, relation="unknown", note=f"linked from {host}")
        self.store.add_evidence(institution_id, asset_id=target_id, kind="hub_link", detail=f"C:{position}:{other['name']}"[:200], source_url=page_url, source_asset_id=domain_asset_id, channel="", observed_via="live", run_id=run_id)
        whose = f"names the look-alike {other['name']}" if lookalike else f"belongs to {other['name']} ({chain[0] if chain else 'another authority'})"
        self.store.add_review_item(
            institution_id, kind="candidate_account", title=f"{host} links {ref.key} ({position}); it {whose}"[:300], url=ref.url, asset_id=target_id, entity_id=entity_id, connector="official_site", run_id=run_id,
            detail="" if lookalike else f"Only an approver for {chain[0] if chain else other['name']} (GURU_INTELLIGENCE_ENTITY_APPROVERS) can confirm it; a link from {host} does not settle which accounts are theirs.",
        )
        result.evidence += 1
        if ref.key not in result.held:
            result.held.append(ref.key)
        if created:
            result.new_assets.append(ref.key)
        return target_id

    @staticmethod
    def _note_raised(changes: list[dict[str, Any]], result: HarvestResult) -> None:
        result.raised.extend(change["asset_key"] for change in changes if GRADE_RANK.get(change["to"], 1) > GRADE_RANK.get(change["from"], 1) and change["asset_key"] not in result.new_assets)

    async def _retrieve(self, institution_id: str, url: str, result: HarvestResult, *, conditional: bool | None = None) -> Retrieval:
        use_cache = self.conditional if conditional is None else conditional
        state = self.store.fetch_state(institution_id, url) if use_cache else None
        result.requests += 1
        return await self.fetcher.retrieve(url, etag=(state or {}).get("etag"), last_modified=(state or {}).get("last_modified"))

    def _reconfirm(self, institution_id: str, domain_asset_id: str, page_url: str, run_id: str | None, result: HarvestResult, *, home_id: str | None = None) -> set[str]:
        """Repeat the latest official-link observation from an unchanged page (a 304 answer).

        An official link recorded before the site's scope was checked, to an
        account now filed under another authority's entity, is not repeated:
        with nothing left to repeat the page is read afresh, and the link
        is held back and withdrawn like any other.
        """

        latest: dict[str, dict[str, object]] = {}
        for item in self.store.links_from(institution_id, domain_asset_id, source_urls=[page_url]):
            latest[item["asset_id"]] = item
        foreign = _Scope.load(self.store, institution_id, home_id).foreign if latest else {}
        linked = self.store.get_assets(institution_id, latest) if latest else {}
        touched: set[str] = set()
        for asset_id, item in latest.items():
            if item["polarity"] != "supports" or (linked.get(asset_id) or {}).get("entity_id") in foreign:
                continue
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="official_link", detail=str(item["detail"]), source_url=page_url, source_asset_id=domain_asset_id, channel=str(item["channel"]), observed_via="live", run_id=run_id)
            result.evidence += 1
            touched.add(asset_id)
        return touched

    def _record_removals(self, institution_id: str, domain_asset_id: str, fetched: dict[str, str], seen_on_page: dict[str, set[str]], run_id: str | None, result: HarvestResult) -> set[str]:
        """A page fetched cleanly that no longer links an account it used to link refutes that link.

        ``fetched`` maps each page read to the sha256 of its body.
        """

        removed: set[str] = set()
        linked_before: dict[tuple[str, str], str] = {}
        for item in self.store.links_from(institution_id, domain_asset_id, source_urls=fetched):
            linked_before[(item["asset_id"], item["source_url"])] = item["polarity"]
        linked_assets = self.store.get_assets(institution_id, {asset_id for asset_id, _ in linked_before})
        for (asset_id, page_url), polarity in linked_before.items():
            asset = linked_assets.get(asset_id)
            if asset is None or polarity != "supports" or asset["asset_key"] in seen_on_page.get(page_url, set()):
                continue
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="official_link", polarity="refutes", detail=f"removed:{page_url}"[:500], source_url=page_url, source_asset_id=domain_asset_id, channel=f"site:{result.domain}", observed_via="live", run_id=run_id, raw_sha256=fetched.get(page_url))
            result.evidence += 1
            removed.add(asset_id)
        return removed

    def _liveness(self, institution_id: str, asset_id: str, retrieval: Retrieval, run_id: str | None) -> None:
        digest = hashlib.sha256(retrieval.body).hexdigest() if retrieval.ok else None
        if retrieval.outcome in {"ok", "not_modified"}:
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="liveness", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=run_id, raw_sha256=digest)
        # unresolved (the name is gone) and unreachable (nothing listens) count toward dead like not_found.
        elif retrieval.outcome in {"not_found", "gone", "blocked", "login_wall", "robots", "server_error", "timeout", "not_public", "unresolved", "unreachable"}:
            self.store.add_evidence(institution_id, asset_id=asset_id, kind="liveness", polarity="refutes", detail=f"{retrieval.outcome}:{retrieval.http_status}", source_url=retrieval.url, channel="fetch", observed_via="live", run_id=run_id)

    def _integrity(self, institution_id: str, asset_id: str, report: IntegrityReport, page_url: str, host: str, run_id: str | None, result: HarvestResult, body: bytes = b"") -> None:
        clean = report.status == CLEAN
        self.store.add_evidence(
            institution_id, asset_id=asset_id, kind="integrity", polarity="supports" if clean else "refutes", detail=f"{report.status}:{';'.join(report.signals)}"[:500],
            source_url=page_url, channel=f"site:{host}", observed_via="live", run_id=run_id, raw_sha256=hashlib.sha256(body).hexdigest() if body else None,
        )
        result.evidence += 1
        if result.integrity in {"unknown", CLEAN}:
            result.integrity = report.status
        if not clean:
            result.incidents.append({"kind": f"site_{report.status}", "target": host, "page": page_url, "signals": report.signals, "examples": report.spam_links[:5]})

    @staticmethod
    def _candidate_pages(home: PageStructure, host: str, listed: list[str] | tuple[str, ...] = (), *, guess: bool = True) -> list[str]:
        """Contact pages the homepage links, then those only its sitemap lists (``listed``), else two guesses."""

        found: list[str] = []
        for link in home.links:
            if link.position == HIDDEN or _host(link.href) != host:
                continue
            if is_contact_page(link.href) or _CONTACT.search(link.text):
                clean = link.href.split("#", 1)[0]
                if clean.rstrip("/") != home.url.rstrip("/") and clean not in found:
                    found.append(clean)
        for url in listed:
            if url.rstrip("/") not in {home.url.rstrip("/"), *(item.rstrip("/") for item in found)}:
                found.append(url)
        if not found and guess:
            found = [urljoin(home.url, path) for path in _GUESSES[:2]]
        return found

    async def _sitemap_pages(self, home: PageStructure, host: str, result: HarvestResult, *, room: int) -> tuple[list[str], int]:
        """(contact and follow-us pages the site's sitemaps list, sitemap requests made).

        A site's accounts are often on a page its homepage does not link. The
        sitemaps robots.txt declares, and /sitemap.xml, are read (at most two
        documents, an index's page sitemaps first) only while a page fetch
        still fits in ``room`` after the read. Pages on other hosts are ignored.
        """

        queue = list(dict.fromkeys([url for url in self.fetcher.sitemaps(home.url) if _host(url) == host] + [urljoin(home.url, "/sitemap.xml")]))
        found: list[str] = []
        done: set[str] = set()
        while queue and len(done) < MAX_SITEMAP_READS and room - len(done) > 1:
            url = queue.pop(0)
            done.add(url)
            result.requests += 1
            retrieval = await self.fetcher.retrieve(url, accept=frozenset({"xml"}), max_bytes=MAX_SITEMAP_BYTES)
            if not retrieval.ok or _host(retrieval.url) != host:
                continue
            listed, nested = parse_sitemap(retrieval.body)
            for page in listed:
                segments = [segment for segment in urlparse(page).path.split("/") if segment]
                clean = page.split("#", 1)[0]
                if _host(page) == host and (is_contact_page(page) or (segments and _SOCIAL_PAGE.search(segments[-1]))) and clean not in found:
                    found.append(clean)
            # An index's own sitemaps are read next, the pages sitemap ("page-sitemap.xml") first.
            queue[:0] = sorted((item for item in dict.fromkeys(nested) if _host(item) == host and item not in done), key=lambda item: "page" not in item.lower())
        return found[: max(0, room - len(done))], len(done)

    @staticmethod
    def _identity_links(structure: PageStructure, page_url: str) -> list[tuple[str, str, str]]:
        """(href, position, link text) of links that declare identity on this page."""

        contact_page = is_contact_page(page_url)
        out: dict[tuple[str, str], str] = {}
        for link in structure.links:
            if link.position == HIDDEN:
                continue
            if link.position in IDENTITY_POSITIONS or link.position == HEAD_LINK or (contact_page and link.position == BODY):
                position = "rel_me" if link.position == HEAD_LINK else ("contact_page" if link.position == BODY else link.position)
                out.setdefault((link.href, position), link.text)
        for href in structure.same_as:
            out.setdefault((href, "same_as"), "")
        return [(href, position, text) for (href, position), text in out.items()]


@dataclass(slots=True)
class _Scope:
    """What a domain may vouch for: its own entity and that entity's units, never another authority's entities or a look-alike."""

    home: dict[str, Any] | None
    entities: dict[str, dict[str, Any]]
    foreign: dict[str, dict[str, Any]]  # entity id -> entity, in the map's order

    @classmethod
    def load(cls, store: MapStore, institution_id: str, home_id: str | None) -> "_Scope":
        entities = {str(entity["entity_id"]): entity for entity in store.list_entities(institution_id, limit=5000)}
        home_chain = set(authorities(entities.get, home_id))
        foreign = {entity_id: entity for entity_id, entity in entities.items() if not within(entities.get, entity_id, home_id) and set(authorities(entities.get, entity_id)) != home_chain}
        return cls(entities.get(home_id) if home_id else None, entities, foreign)

    def owner(self, existing: dict[str, Any] | None, ref: Any, text: str) -> dict[str, Any] | None:
        """The other entity a link is about: its account is already mapped under it, or its handle or link text names it."""

        if existing is not None and existing.get("entity_id") in self.foreign:
            return self.foreign[existing["entity_id"]]
        if self.home is not None and names_entity(self.home, handle=ref.handle, title=text):
            return None
        return next((entity for entity in self.foreign.values() if names_entity(entity, handle=ref.handle, title=text)), None)


# A contact or about page itself, not a page beneath it (/about/principal lists people).
_CONTACT_PAGE = re.compile(r"^(?:contact|contact[-_]?us|about|about[-_]?us|connect(?:[-_]with[-_]us)?|follow[-_]?us|social(?:[-_]media)?|reach[-_]?us|get[-_]in[-_]touch)(?:\.[a-z]{2,5})?$", re.IGNORECASE)


def is_contact_page(url: str) -> bool:
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    return bool(segments) and bool(_CONTACT_PAGE.match(segments[-1]))


def vouchable(entity: dict[str, Any] | None, ref: Any, position: str, text: str) -> bool:
    """Whether an official page's link may vouch for this account as the institution's.

    Header, navigation, footer, rel=me and sameAs links do; a link in the
    body of a contact page only when the account itself carries the
    institution's name (a principal's LinkedIn listed there does not); and
    accounts that always belong to a person only on that same condition.
    """

    if ref.key.startswith("whatsapp:") and not ref.key.startswith("whatsapp:group:"):
        return False  # a phone number, not an account the map should publish
    if position == "contact_page" or person_shaped(ref.key):
        return entity is not None and names_entity(entity, handle=ref.handle, title=text)
    return True


def identity_links(structure: PageStructure, page_url: str) -> list[tuple[str, str, str]]:
    """(href, position, link text) of the links a page uses to declare its own accounts."""

    return OfficialSiteHarvester._identity_links(structure, page_url)


def best(grades: list[str]) -> str:
    return max(grades, key=lambda value: GRADE_RANK.get(value, 1)) if grades else "unrated"


__all__ = ["ANCHORING_GRADES", "HarvestResult", "OfficialSiteHarvester", "identity_links", "is_contact_page", "vouchable"]
