"""Feeds published for machines: an official site's RSS/Atom feed and YouTube channel feeds.

A feed tells the map two things cheaply: that the account or site behind it
still exists (a YouTube channel whose feed is gone twice, a day apart, is
dead) and when it last published, so dormant accounts can be told from
active ones. Requests are conditional, so an unchanged feed costs a 304.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .base import ConnectorContext, ConnectorResult, Lead
from .common import FAILED_OUTCOMES, parse_feed

FEED_TYPES = frozenset({"feed", "xml"})
_YOUTUBE_FEED = re.compile(r"^https://www\.youtube\.com/feeds/videos\.xml\?channel_id=(UC[\w-]{22})$")
DORMANT_AFTER = timedelta(days=365)


def youtube_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


@dataclass(slots=True)
class FeedConnector:
    active: bool = False
    name: str = "feed"
    access_mode: str = "public_feed"
    budget_key: str = "feed"
    max_grade: str = "C"
    default_interval: int = 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        leads = []
        for asset in context.store.iter_assets(context.institution_id, platform="youtube", kind="account"):
            if asset["asset_key"].startswith("youtube:channel:") and asset["grade"] != "D":
                url = youtube_feed_url(asset["asset_key"].removeprefix("youtube:channel:"))
                if url not in existing:
                    leads.append(Lead(self.name, url, entity_id=asset["entity_id"], asset_id=asset["asset_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval))
        return leads

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        url = str(source["target"])
        youtube = _YOUTUBE_FEED.match(url)
        state = context.store.fetch_state(url) or {}
        # YouTube pages are never fetched; its channel feed is a machine
        # endpoint published for exactly this, and robots.txt still applies.
        retrieval = await context.fetcher.retrieve(url, accept=FEED_TYPES, etag=state.get("etag"), last_modified=state.get("last_modified"), check_domain=youtube is None)
        keep = retrieval.outcome in {"ok", "not_modified"}
        context.store.record_fetch(url, outcome=retrieval.outcome, etag=retrieval.etag if keep else None, last_modified=retrieval.last_modified if keep else None, content_sha256=None)
        asset_id = source.get("asset_id")
        result = ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in FAILED_OUTCOMES, etag=retrieval.etag, last_modified=retrieval.last_modified)
        if youtube and asset_id:
            # Only a channel's own feed speaks to the channel being alive.
            if keep:
                context.store.add_evidence(context.institution_id, asset_id=str(asset_id), kind="liveness", detail=f"{retrieval.outcome}:feed", source_url=url, channel="feed", observed_via="live", run_id=context.run_id)
                result.touched.add(str(asset_id))
            elif retrieval.outcome in {"not_found", "gone"}:
                context.store.add_evidence(context.institution_id, asset_id=str(asset_id), kind="liveness", polarity="refutes", detail=f"{retrieval.outcome}:feed", source_url=url, channel="feed", observed_via="live", run_id=context.run_id)
                result.touched.add(str(asset_id))
        if retrieval.outcome != "ok":
            if retrieval.outcome in {"not_found", "gone"} and source.get("origin") == "lead":
                result.prune = True
            return result
        parsed = parse_feed(retrieval.body)
        if parsed is None:
            return ConnectorResult(outcome="not_a_feed", prune=source.get("origin") == "lead")
        _, items = parsed
        dates = [item.published_at for item in items if item.published_at is not None]
        if asset_id and dates:
            latest = max(dates)
            context.store.set_observation(context.institution_id, str(asset_id), last_activity_at=latest.isoformat())
            if context.now - latest > DORMANT_AFTER:
                result.notes.append(f"dormant: nothing published since {latest.date().isoformat()}")
        result.notes.append(f"{len(items)} items")
        return result


__all__ = ["FEED_TYPES", "FeedConnector", "youtube_feed_url"]
