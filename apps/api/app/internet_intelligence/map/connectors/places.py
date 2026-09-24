"""Two free passive sources from the sweep: OpenStreetMap and Google Play.

* OpenStreetMap: the Nominatim API, used within its usage policy: an
  identifying User-Agent with the operator's contact
  (SAFFRON_INTELLIGENCE_CRAWLER_CONTACT is required to enable it), at least
  15 seconds between requests, one request a month per entity, and no
  repeated searches: the object a search chose is kept on the source and
  re-read with ``/lookup`` from then on. A campus mapped in OSM often
  carries its website and social accounts as tags; like Wikidata, OSM is
  edited by anyone, so its tags are one community channel (C) and never an
  anchor. What it contributes is © OpenStreetMap contributors, under the
  ODbL, and is attributed wherever it is shown.
* Google Play: the public listing page of an app the map already knows
  (usually from an official site's footer badge), fetched through the
  robots-aware fetcher (up to 3 MB: listings run past 1 MB). An app whose
  listing is gone is recorded as dead; a listing whose developer website
  or support address is on one of the entity's official domains links
  back to it (C, one channel). Play is not a social platform and nothing
  here logs in or searches the store itself: new apps are found through
  the licensed search index (google_play_search), matched on the
  developer's name, never through play.google.com/store/search.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ...search import IntelligenceSearchUnavailable
from ..assets import ACCOUNT, DOMAIN, GROUP, asset_ref
from .base import ConnectorContext, ConnectorResult, Lead
from .common import FAILED_OUTCOMES, ApiClient, _compact, names_entity, person_shaped, unique

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_LOOKUP = "https://nominatim.openstreetmap.org/lookup"
# Nominatim's usage policy caps requests at one a second; the map is a
# background job with a month between lookups, so it keeps well clear of that.
NOMINATIM_MIN_GAP_SECONDS = 15.0
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
# The chosen object is kept in the source's validator column as "osm:<type>/<id>" ("osm:" alone: search again).
_CACHED_PLACE = re.compile(r"^osm:(node|way|relation)/(\d{1,20})$")

# OSM tags that name the object's website or one of its accounts, and the URL
# a bare handle stands for. Full URLs are used as they are.
OSM_ACCOUNT_TAGS: dict[str, str] = {
    "website": "{}", "contact:website": "{}", "url": "{}",
    "contact:facebook": "https://www.facebook.com/{}", "facebook": "https://www.facebook.com/{}",
    "contact:instagram": "https://www.instagram.com/{}/", "instagram": "https://www.instagram.com/{}/",
    "contact:twitter": "https://x.com/{}", "twitter": "https://x.com/{}",
    "contact:youtube": "https://www.youtube.com/{}", "youtube": "https://www.youtube.com/{}",
    "contact:linkedin": "https://www.linkedin.com/company/{}/", "linkedin": "https://www.linkedin.com/company/{}/",
    "contact:telegram": "https://t.me/{}", "telegram": "https://t.me/{}",
}
_OSM_TYPES = frozenset({"node", "way", "relation"})


def _tag_url(template: str, value: str) -> str:
    value = value.strip()
    if value.lower().startswith(("http://", "https://")) or template == "{}":
        return value
    return template.format(value.lstrip("@").strip("/"))


@dataclass(slots=True)
class OpenStreetMapConnector:
    """Find each entity on OpenStreetMap (Nominatim) and record the website and accounts its tags list (community data: C)."""

    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "openstreetmap"
    access_mode: str = "official_api"
    budget_key: str = "openstreetmap"
    max_grade: str = "C"
    default_interval: int = 30 * 86400
    max_entities: int = 40
    min_gap_seconds: float = NOMINATIM_MIN_GAP_SECONDS
    _last_request: float = field(default=0.0, repr=False)

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        entities = [entity for entity in context.entities() if entity["kind"] != "lookalike"][: self.max_entities]
        return [
            Lead(self.name, entity["entity_id"], entity_id=entity["entity_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval)
            for entity in entities if entity["entity_id"] not in existing
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        entity = context.store.get_entity(context.institution_id, str(source["target"]))
        if entity is None:
            return ConnectorResult(outcome="entity_missing", prune=True)
        wait = self._last_request + self.min_gap_seconds - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()
        cached = _CACHED_PLACE.match(str(source.get("etag") or ""))
        if cached:
            # The object chosen before is re-read by its ID, not searched for again.
            response = await self.client.request(NOMINATIM_LOOKUP, params={"osm_ids": f"{cached.group(1)[0].upper()}{cached.group(2)}", "format": "jsonv2", "extratags": 1, "namedetails": 1})
        else:
            response = await self.client.request(NOMINATIM_SEARCH, params={"q": entity["name"][:200], "format": "jsonv2", "extratags": 1, "namedetails": 1, "limit": 5})
        if not response.ok:
            return ConnectorResult(outcome=response.outcome, failed=True)
        places = response.json()
        chosen = self._choose(context, entity, places if isinstance(places, list) else [])
        if chosen is None:
            # A cached object that is gone or renamed is searched for again next time.
            return ConnectorResult(outcome="place_changed" if cached else "no_confident_place", etag="osm:" if cached else None)
        osm = f"{chosen['osm_type']}/{chosen['osm_id']}"
        source_url = f"https://www.openstreetmap.org/{osm}"
        tags = chosen.get("extratags") if isinstance(chosen.get("extratags"), dict) else {}
        result = ConnectorResult(outcome="ok", etag=f"osm:{osm}", notes=[f"data {OSM_ATTRIBUTION}"])
        for tag, template in OSM_ACCOUNT_TAGS.items():
            value = tags.get(tag)
            if not isinstance(value, str) or not value.strip():
                continue
            for part in value.split(";")[:3]:
                try:
                    ref = asset_ref(_tag_url(template, part)[:300])
                except ValueError:
                    continue
                if ref.kind not in {ACCOUNT, GROUP, DOMAIN} or person_shaped(ref.key) or context.store.is_suppressed(context.institution_id, ref.key):
                    continue
                asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=entity["entity_id"], relation="official", note=f"tagged on OpenStreetMap {osm}")
                context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="community_record", detail=f"osm:{osm}:{tag}", source_url=source_url, channel="openstreetmap", observed_via="index", run_id=context.run_id)
                result.touched.add(asset_id)
                if created:
                    result.new_assets.append(ref.key)
                    result.yield_count += 1
        return result

    @staticmethod
    def _choose(context: ConnectorContext, entity: Mapping[str, Any], places: list[Any]) -> dict[str, Any] | None:
        """The one place named like the entity (and no look-alike); when several are, the one whose website is already ours."""

        names = {_compact(str(name)) for name in [entity["name"], *(entity.get("names") or [])] if len(_compact(str(name))) >= 3}
        rivals = {_compact(str(other["name"])) for other in context.entities() if other["kind"] == "lookalike"}
        ours = {domain["asset_key"] for domain in context.store.iter_assets(context.institution_id, kind="domain") if domain["entity_id"] == entity["entity_id"]}
        named: list[dict[str, Any]] = []
        for place in places:
            if not isinstance(place, dict) or place.get("osm_type") not in _OSM_TYPES or not str(place.get("osm_id", "")).isdigit():
                continue
            details = place.get("namedetails") if isinstance(place.get("namedetails"), dict) else {}
            labels = {_compact(str(value)) for value in [place.get("name"), *details.values()] if isinstance(value, str)}
            if labels & names and not labels & rivals:
                named.append(place)
        if len(named) == 1:
            return named[0]
        for place in named:
            tags = place.get("extratags") if isinstance(place.get("extratags"), dict) else {}
            for tag in ("website", "contact:website", "url"):
                try:
                    if isinstance(tags.get(tag), str) and asset_ref(tags[tag]).key in ours:
                        return place
                except ValueError:
                    continue
        return None


_HREF = re.compile(r"""href\s*=\s*["']([^"'<>\s]+)["']""", re.IGNORECASE)


@dataclass(slots=True)
class GooglePlayConnector:
    """Re-check each known Google Play app listing: is it still published, and does its developer point at an official domain?"""

    active: bool = False
    name: str = "google_play"
    access_mode: str = "public_page"
    budget_key: str = "fetch"
    max_grade: str = "C"
    default_interval: int = 14 * 86400
    max_bytes: int = 3_000_000

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        return [
            Lead(self.name, asset["asset_id"], entity_id=asset["entity_id"], asset_id=asset["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.default_interval)
            for asset in context.store.iter_assets(context.institution_id, platform="google_play", kind="account")
            if asset["asset_id"] not in existing and asset["grade"] != "D"
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        asset = context.store.get_asset(context.institution_id, str(source["target"]))
        if asset is None or asset["platform"] != "google_play":
            return ConnectorResult(outcome="asset_missing", prune=True)
        # A listing page is 1.2-1.4 MB of markup, over the fetcher's usual 1 MB cap.
        retrieval = await context.fetcher.retrieve(asset["url"], max_bytes=self.max_bytes)
        result = ConnectorResult(outcome=retrieval.outcome, touched={asset["asset_id"]}, failed=retrieval.outcome in FAILED_OUTCOMES)
        if retrieval.outcome in {"not_found", "gone", "blocked", "login_wall", "robots"}:
            # Gone twice a day apart is dead; refused or walled is shown as
            # blocked with when it was last seen, never as dead.
            context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", polarity="refutes", detail=f"{retrieval.outcome}:{retrieval.http_status or ''}", source_url=asset["url"], channel="google_play", observed_via="live", run_id=context.run_id)
            return result
        if not retrieval.ok:
            return result
        context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", detail=f"ok:{retrieval.http_status or 200}", source_url=asset["url"], channel="google_play", observed_via="live", run_id=context.run_id)
        official_hosts = {domain["asset_key"].removeprefix("web:") for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official") if domain["entity_id"] == asset["entity_id"] and domain["grade"] != "D"}
        for href in unique(_HREF.findall(retrieval.text)):
            if href.lower().startswith("mailto:"):
                host = href.split("@", 1)[-1].split("?", 1)[0].lower()
                how = "support address"
            else:
                host = (urlparse(href).hostname or "").lower()
                how = "developer website"
            host = host.removeprefix("www.")
            if host and any(host == official or host.endswith("." + official) for official in official_hosts):
                context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="backlink", detail=f"listing's {how} is on {host}", source_url=asset["url"], channel="google_play", observed_via="live", run_id=context.run_id)
                break
        return result


# App listings and developer pages; Play's own search (/store/search) is never a result the map uses.
_PLAY_APP_PATH = re.compile(r"^/store/apps/(?:details|dev|developer)/?$")
# How a result names an app's developer: "Android Apps by <developer> on Google Play" (a developer page) or "Offered by <developer>".
_DEVELOPER = re.compile(r"(?:Android Apps by|Offered by|Developed by|Developer:)\s+(.+?)(?:\s+on Google Play\b|\s+[|•·-]\s|[\n.]|$)", re.IGNORECASE)


@dataclass(slots=True)
class GooglePlaySearchConnector:
    """Find an entity's apps through the licensed search index (target: the entity ID), matched on the developer's name.

    The query is restricted to play.google.com/store/apps and only listings
    and developer pages count, never Play's own search. An app counts only
    when the developer named in the result is the entity itself (the rule
    accounts follow: its name, or its name beside institutional words) and
    no look-alike; it is then a C-grade candidate from one channel, which
    the google_play connector re-checks for a link back to an official domain.
    """

    active: bool = False
    name: str = "google_play_search"
    access_mode: str = "search_index"
    budget_key: str = "search"
    max_grade: str = "C"
    default_interval: int = 30 * 86400
    max_entities: int = 40
    results_per_query: int = 10

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        if context.search is None:
            return []
        existing = context.store.source_targets(context.institution_id, self.name)
        entities = [entity for entity in context.entities() if entity["kind"] != "lookalike"][: self.max_entities]
        return [Lead(self.name, entity["entity_id"], entity_id=entity["entity_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval) for entity in entities if entity["entity_id"] not in existing]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.search is None:
            return ConnectorResult(outcome="search_disabled", failed=True)
        entity = context.store.get_entity(context.institution_id, str(source["target"]))
        if entity is None:
            return ConnectorResult(outcome="entity_missing", prune=True)
        provider = getattr(context.search, "provider_name", "search")
        try:
            hits = await context.search.search(f'"{entity["name"][:200]}" site:play.google.com/store/apps', max_results=self.results_per_query, include_domains=["play.google.com"])
        except IntelligenceSearchUnavailable as exc:
            return ConnectorResult(outcome="search_unavailable", failed=True, notes=[str(exc)[:200]])
        rivals = [other for other in context.entities() if other["kind"] == "lookalike"]
        result = ConnectorResult(outcome="ok", cost=1.0)
        for hit in hits:
            parsed = urlparse(hit.url)
            if (parsed.hostname or "").lower() != "play.google.com" or not _PLAY_APP_PATH.match(parsed.path):
                continue
            try:
                ref = asset_ref(hit.url)
            except ValueError:
                continue
            found = _DEVELOPER.search(hit.title) or _DEVELOPER.search(hit.snippet)
            developer = found.group(1).strip()[:120] if found else ""
            if ref.kind != ACCOUNT or not developer or context.store.is_suppressed(context.institution_id, ref.key):
                continue
            if not names_entity(entity, handle="", title=developer) or any(names_entity(rival, handle="", title=developer) for rival in rivals):
                continue
            asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=entity["entity_id"], relation="unknown", note=f"found by searching Google Play listings (developer {developer})")
            context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="search_snippet", detail=f"{provider}: developer {developer}", source_url=ref.url, channel=f"search:{provider}", observed_via="index", run_id=context.run_id)
            result.touched.add(asset_id)
            if created:
                result.new_assets.append(ref.key)
                result.yield_count += 1
        return result


__all__ = ["OSM_ATTRIBUTION", "GooglePlayConnector", "GooglePlaySearchConnector", "OSM_ACCOUNT_TAGS", "OpenStreetMapConnector"]
