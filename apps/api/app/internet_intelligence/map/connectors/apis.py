"""Documented public APIs: the YouTube Data API, Wikidata and IndianKanoon.

Each is used within its terms and only with the institution's own key where
one is needed. None of them can make anything official on its own: YouTube
confirms a channel exists and what it says about itself, Wikidata is
community-edited (one channel, C), and court records only ever go to a
person for review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..assets import asset_ref
from .base import ConnectorContext, ConnectorResult, Lead
from .common import ApiClient, _compact, unique

YOUTUBE_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
INDIANKANOON_SEARCH = "https://api.indiankanoon.org/search/"
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def _our_entities(context: ConnectorContext, limit: int, kinds: frozenset[str] | None = None) -> list[dict[str, Any]]:
    return [entity for entity in context.entities() if entity["kind"] != "lookalike" and (kinds is None or entity["kind"] in kinds)][:limit]


def _existing(context: ConnectorContext, name: str) -> set[str]:
    return context.store.source_targets(context.institution_id, name)


@dataclass(slots=True)
class YouTubeConnector:
    """channels.list for each mapped channel: existence, subscriber count, and the links it declares."""

    api_key: str = field(default="", repr=False)
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "youtube"
    access_mode: str = "official_api"
    budget_key: str = "youtube_api"
    max_grade: str = "C"
    default_interval: int = 7 * 86400

    def enabled(self) -> bool:
        return self.active and bool(self.api_key)

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = _existing(context, self.name)
        return [
            Lead(self.name, asset["asset_id"], entity_id=asset["entity_id"], asset_id=asset["asset_id"], hops=0, work_class="recheck", origin="recurring", interval_seconds=self.default_interval)
            for asset in context.store.iter_assets(context.institution_id, platform="youtube", kind="account")
            if asset["asset_id"] not in existing and asset["grade"] != "D"
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        asset = context.store.get_asset(context.institution_id, str(source["target"]))
        if asset is None or asset["platform"] != "youtube":
            return ConnectorResult(outcome="asset_missing", prune=True)
        key = asset["asset_key"].removeprefix("youtube:")
        params: dict[str, Any] = {"part": "snippet,statistics", "key": self.api_key, "maxResults": 1}
        if key.startswith("channel:"):
            params["id"] = key.removeprefix("channel:")
        elif key.startswith("@"):
            params["forHandle"] = key
        elif key.startswith("user:"):
            params["forUsername"] = key.removeprefix("user:")
        else:
            return ConnectorResult(outcome="unsupported_key", prune=True)
        response = await self.client.request(YOUTUBE_CHANNELS, params=params)
        if not response.ok:
            return ConnectorResult(outcome=response.outcome, failed=True)
        payload = response.json() or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        result = ConnectorResult(outcome="ok", touched={asset["asset_id"]})
        if not items:
            # The API is the platform's own answer: the channel does not exist (any more).
            context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", polarity="refutes", detail="not_found:api", source_url=asset["url"], channel="youtube_api", observed_via="live", run_id=context.run_id)
            result.outcome = "not_found"
            return result
        channel = items[0] if isinstance(items[0], dict) else {}
        snippet = channel.get("snippet") or {}
        statistics = channel.get("statistics") or {}
        channel_id = str(channel.get("id") or "")[:40]
        followers = None if statistics.get("hiddenSubscriberCount") else _int(statistics.get("subscriberCount"))
        context.store.set_observation(context.institution_id, asset["asset_id"], platform_id=channel_id or None, followers=followers, followers_source="youtube_api" if followers is not None else None)
        title = str(snippet.get("title") or "")[:120]
        context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="liveness", detail="ok:api", source_url=asset["url"], channel="youtube_api", observed_via="live", run_id=context.run_id)
        context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="api_identity", detail=f"youtube {channel_id}: {title}", source_url=asset["url"], channel="youtube_api", observed_via="live", run_id=context.run_id)
        # A channel that names one of the entity's official sites in its description links back to it.
        official_hosts = {domain["asset_key"].removeprefix("web:") for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official") if domain["entity_id"] == asset["entity_id"]}
        for url in unique(_URL.findall(str(snippet.get("description") or ""))):
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
            if host in official_hosts:
                context.store.add_evidence(context.institution_id, asset_id=asset["asset_id"], kind="backlink", detail=f"channel description links {host}", source_url=asset["url"], channel="youtube_api", observed_via="live", run_id=context.run_id)
                break
        if key.startswith(("@", "user:")) and channel_id:
            twin = context.store.find_asset(context.institution_id, f"youtube:channel:{channel_id}")
            if twin and twin["asset_id"] != asset["asset_id"]:
                result.notes.append(f"same channel as {twin['asset_key']}")
        return result


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Wikidata properties that name an account on a platform, and the URL each makes.
WIKIDATA_ACCOUNTS: dict[str, str] = {
    "P856": "{}",  # official website
    "P2003": "https://www.instagram.com/{}/",
    "P2013": "https://www.facebook.com/{}",
    "P4003": "https://www.facebook.com/{}",
    "P2002": "https://x.com/{}",
    "P2397": "https://www.youtube.com/channel/{}",
    "P11245": "https://www.youtube.com/@{}",
    "P4264": "https://www.linkedin.com/company/{}/",
    "P3789": "https://t.me/{}",
    "P2037": "https://github.com/{}",
}


@dataclass(slots=True)
class WikidataConnector:
    """Find each entity's Wikidata item and record the website and accounts it lists (community data: C)."""

    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "wikidata"
    access_mode: str = "official_api"
    budget_key: str = "wikidata"
    max_grade: str = "C"
    default_interval: int = 30 * 86400
    max_entities: int = 40

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 2.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = _existing(context, self.name)
        return [
            Lead(self.name, entity["entity_id"], entity_id=entity["entity_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval)
            for entity in _our_entities(context, self.max_entities) if entity["entity_id"] not in existing
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        entity = context.store.get_entity(context.institution_id, str(source["target"]))
        if entity is None:
            return ConnectorResult(outcome="entity_missing", prune=True)
        found = await self.client.request(WIKIDATA_API, params={"action": "wbsearchentities", "search": entity["name"][:200], "language": "en", "type": "item", "limit": 7, "format": "json"})
        if not found.ok:
            return ConnectorResult(outcome=found.outcome, failed=True)
        candidates = [str(item.get("id")) for item in ((found.json() or {}).get("search") or []) if isinstance(item, dict) and re.fullmatch(r"Q\d+", str(item.get("id")))]
        if not candidates:
            return ConnectorResult(outcome="no_item")
        got = await self.client.request(WIKIDATA_API, params={"action": "wbgetentities", "ids": "|".join(candidates), "props": "labels|aliases|claims", "languages": "en", "format": "json"})
        if not got.ok:
            return ConnectorResult(outcome=got.outcome, failed=True)
        items = ((got.json() or {}).get("entities") or {})
        item_id = self._choose(context, entity, items)
        if item_id is None:
            return ConnectorResult(outcome="no_confident_item", notes=[f"{len(items)} candidates, none matched by name and website"])
        claims = items[item_id].get("claims") or {}
        result = ConnectorResult(outcome="ok", cost=2.0)
        for prop, template in WIKIDATA_ACCOUNTS.items():
            for value in _claim_values(claims.get(prop)):
                try:
                    ref = asset_ref(template.format(value))
                except ValueError:
                    continue
                if ref.kind not in {"account", "group", "domain"} or context.store.is_suppressed(context.institution_id, ref.key):
                    continue
                asset_id, created = context.store.upsert_asset(context.institution_id, ref, entity_id=entity["entity_id"], relation="official", note=f"listed on Wikidata {item_id}")
                context.store.add_evidence(context.institution_id, asset_id=asset_id, kind="community_record", detail=f"wikidata:{item_id}:{prop}", source_url=f"https://www.wikidata.org/wiki/{item_id}", channel="wikidata", observed_via="index", run_id=context.run_id)
                result.touched.add(asset_id)
                if created:
                    result.new_assets.append(ref.key)
                    result.yield_count += 1
        return result

    @staticmethod
    def _choose(context: ConnectorContext, entity: Mapping[str, Any], items: Mapping[str, Any]) -> str | None:
        """The one item named like the entity; when several are, the one whose website is already ours."""

        names = {_compact(str(name)) for name in [entity["name"], *(entity.get("names") or [])] if len(_compact(str(name))) >= 3}
        rivals = {_compact(str(other["name"])) for other in context.entities() if other["kind"] == "lookalike"}
        ours = {domain["asset_key"] for domain in context.store.iter_assets(context.institution_id, kind="domain") if domain["entity_id"] == entity["entity_id"]}
        named: list[str] = []
        for item_id, item in items.items():
            if not isinstance(item, dict):
                continue
            labels = {_compact(str(label.get("value", ""))) for label in (item.get("labels") or {}).values() if isinstance(label, dict)}
            labels |= {_compact(str(alias.get("value", ""))) for values in (item.get("aliases") or {}).values() if isinstance(values, list) for alias in values if isinstance(alias, dict)}
            if labels & names and not labels & rivals:
                named.append(item_id)
        if len(named) == 1:
            return named[0]
        for item_id in named:
            for site in _claim_values((items[item_id].get("claims") or {}).get("P856")):
                try:
                    if asset_ref(site).key in ours:
                        return item_id
                except ValueError:
                    continue
        return None


def _claim_values(claims: Any) -> list[str]:
    values: list[str] = []
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
            continue
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, str) and value.strip():
            values.append(value.strip()[:300])
    return values


@dataclass(slots=True)
class CourtRecordsConnector:
    """IndianKanoon search for each entity's exact name. Every hit goes to a person; nothing is published or graded."""

    api_token: str = field(default="", repr=False)
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "court_records"
    access_mode: str = "official_api"
    budget_key: str = "indiankanoon"
    max_grade: str = "C"
    default_interval: int = 30 * 86400
    max_entities: int = 20

    def enabled(self) -> bool:
        return self.active and bool(self.api_token)

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = _existing(context, self.name)
        return [
            Lead(self.name, entity["entity_id"], entity_id=entity["entity_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval)
            for entity in _our_entities(context, self.max_entities, frozenset({"organisation", "trust", "institution"})) if entity["entity_id"] not in existing
        ]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        entity = context.store.get_entity(context.institution_id, str(source["target"]))
        if entity is None:
            return ConnectorResult(outcome="entity_missing", prune=True)
        response = await self.client.request(INDIANKANOON_SEARCH, method="POST", data={"formInput": f'"{entity["name"][:200]}"', "pagenum": 0}, headers={"Authorization": f"Token {self.api_token}"})
        if not response.ok:
            return ConnectorResult(outcome=response.outcome, failed=True)
        docs = (response.json() or {}).get("docs") or []
        wanted = _compact(entity["name"])
        result = ConnectorResult(outcome="ok")
        for doc in docs[:20] if isinstance(docs, list) else []:
            if not isinstance(doc, dict) or not str(doc.get("tid", "")).isdigit():
                continue
            title = _TAGS.sub("", str(doc.get("title") or ""))[:300]
            headline = _TAGS.sub("", str(doc.get("headline") or ""))[:500]
            if wanted not in _compact(f"{title} {headline}"):
                continue
            result.review.append({
                "kind": "court_record", "entity_id": entity["entity_id"], "title": title, "url": f"https://indiankanoon.org/doc/{doc['tid']}/",
                "detail": f"{str(doc.get('docsource') or '')[:80]} {str(doc.get('publishdate') or '')[:10]}: {headline}".strip(),
            })
        result.yield_count = 0
        return result


__all__ = ["CourtRecordsConnector", "WIKIDATA_ACCOUNTS", "WikidataConnector", "YouTubeConnector"]
