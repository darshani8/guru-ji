"""Feeds published for machines: an official site's RSS/Atom feed, and news publishers' feeds.

A site's feed tells the map two things cheaply: that the site behind it
still exists and when it last published, so dormant sites can be told from
active ones. Requests are conditional, so an unchanged feed costs a 304.

YouTube channel feeds are not read: youtube.com/robots.txt disallows
``/feeds/videos.xml`` for every crawler, so a channel's last upload is
dated through the YouTube Data API instead (the youtube connector). A
channel feed an official site declares is tried once, through the
robots-aware fetcher, and dropped when robots.txt refuses it.

News feeds (``news_feed``) are English and Kannada publishers' RSS feeds.
Each item is matched against the mapped entities; a mention never changes
a grade or adds an asset. A mention that carries court or controversy
words goes to a person for review; the rest are only counted for the
daily digest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...relevance import topic_tags
from ...store import SENSITIVE_TOPICS
from ..seed import SEEDS_DIR
from .base import ConnectorContext, ConnectorResult, Lead
from .common import FAILED_OUTCOMES, match_entity, parse_feed

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

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        url = str(source["target"])
        youtube = _YOUTUBE_FEED.match(url)
        state = context.store.fetch_state(context.institution_id, url) or {}
        # YouTube pages are never fetched; its channel feed is a machine
        # endpoint published for exactly this, and robots.txt still applies.
        retrieval = await context.fetcher.retrieve(url, accept=FEED_TYPES, etag=state.get("etag"), last_modified=state.get("last_modified"), check_domain=youtube is None)
        keep = retrieval.outcome in {"ok", "not_modified"}
        context.store.record_fetch(context.institution_id, url, outcome=retrieval.outcome, etag=retrieval.etag if keep else None, last_modified=retrieval.last_modified if keep else None, content_sha256=None)
        if youtube:
            # A channel feed speaks for that channel only, whatever page declared it.
            channel = context.store.find_asset(context.institution_id, f"youtube:channel:{youtube.group(1)}")
            asset_id = channel["asset_id"] if channel else None
        else:
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
            if youtube and retrieval.outcome == "robots":
                # youtube.com/robots.txt refuses channel feeds to every crawler; the Data API dates the channel.
                result.prune = True
                result.notes.append("youtube.com/robots.txt disallows channel feeds; the youtube connector reads the channel through the Data API")
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


NEWS_FEEDS = SEEDS_DIR / "news_feeds.tsv"
# Court and controversy words in Kannada. English ones are the "controversy"
# topic (SENSITIVE_TOPICS); Kannada joins case endings to a word
# (ನ್ಯಾಯಾಲಯದಲ್ಲಿ, "in the court"), so these are matched inside words.
KANNADA_SENSITIVE_TERMS: tuple[str, ...] = (
    "ನ್ಯಾಯಾಲಯ", "ಹೈಕೋರ್ಟ್", "ಸುಪ್ರೀಂ ಕೋರ್ಟ್", "ಪ್ರಕರಣ", "ದೂರು", "ಆರೋಪ", "ಪೊಲೀಸ್", "ಬಂಧನ", "ಪ್ರತಿಭಟನೆ", "ಮುಷ್ಕರ", "ವಂಚನೆ", "ರ್ಯಾಗಿಂಗ್", "ಕಿರುಕುಳ", "ಅಮಾನತು",
)
# Phrases that hold a term without its meaning, taken out before matching:
# "ರಕ್ಷಾ ಬಂಧನ" (Raksha Bandhan, the festival) is not "ಬಂಧನ" (arrest). The
# verb "ಬಂಧಿಸ" (arrested) is not a term: it sits inside "ಸಂಬಂಧಿಸಿದ" ("related
# to"), which nearly every Kannada news item uses.
_KANNADA_NOT_SENSITIVE = re.compile(r"ರಕ್ಷಾ[\s\u200c\u200d-]*ಬಂಧನ")  # spaced, hyphenated or joined (zero-width joiners too)


@dataclass(frozen=True, slots=True)
class NewsFeed:
    publisher: str
    language: str
    url: str


def load_news_feeds(path: Path = NEWS_FEEDS) -> tuple[NewsFeed, ...]:
    """The seeded publishers' feeds: tab-separated publisher, language, https URL ('#' starts a comment)."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    feeds = []
    for line in lines:
        parts = [part.strip() for part in line.split("\t")]
        if line.startswith("#") or len(parts) < 3 or parts[0] == "publisher" or not parts[2].startswith("https://"):
            continue
        feeds.append(NewsFeed(parts[0][:120], parts[1][:10], parts[2][:1000]))
    return tuple(feeds)


def sensitive_mention(title: str, summary: str) -> bool:
    """Whether a news item carries court or controversy words (the English "controversy" topic, or Kannada terms)."""

    kannada = _KANNADA_NOT_SENSITIVE.sub(" ", f"{title} {summary}")
    return bool(SENSITIVE_TOPICS & set(topic_tags(summary, title))) or any(term in kannada for term in KANNADA_SENSITIVE_TERMS)


def _read_at(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class NewsFeedConnector:
    """Read a news publisher's RSS feed (target: the feed URL) for items that name a mapped entity.

    A mention is an item whose title or summary names an entity clearly more
    strongly than any look-alike (the margin every connector uses), so "BGS"
    the British Geological Survey is not "BGS College". Mentions are never
    evidence: nothing is graded and no asset is added. One that carries
    court or controversy words goes to review ("news_mention"); the rest are
    counted for the digest, each once (items older than the last read were
    counted then).
    """

    active: bool = False
    feeds: tuple[NewsFeed, ...] = field(default_factory=load_news_feeds)
    name: str = "news_feed"
    access_mode: str = "public_feed"
    budget_key: str = "feed"
    max_grade: str = "C"
    default_interval: int = 86400
    max_bytes: int = 3_000_000
    max_items: int = 100

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        return [Lead(self.name, feed.url, hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval) for feed in self.feeds if feed.url not in existing]

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        if context.fetcher is None:
            return ConnectorResult(outcome="fetching_disabled", failed=True)
        url = str(source["target"])
        state = context.store.fetch_state(context.institution_id, url) or {}
        retrieval = await context.fetcher.retrieve(url, accept=FEED_TYPES, etag=state.get("etag"), last_modified=state.get("last_modified"), max_bytes=self.max_bytes)
        keep = retrieval.outcome in {"ok", "not_modified"}
        context.store.record_fetch(context.institution_id, url, outcome=retrieval.outcome, etag=retrieval.etag if keep else None, last_modified=retrieval.last_modified if keep else None, content_sha256=None)
        result = ConnectorResult(outcome=retrieval.outcome, failed=retrieval.outcome in FAILED_OUTCOMES, etag=retrieval.etag, last_modified=retrieval.last_modified)
        if retrieval.outcome != "ok":
            result.prune = retrieval.outcome in {"not_found", "gone"} and source.get("origin") == "lead"
            return result
        parsed = parse_feed(retrieval.body, limit=self.max_items)
        if parsed is None:
            return ConnectorResult(outcome="not_a_feed", prune=source.get("origin") == "lead")
        title, items = parsed
        publisher = next((feed.publisher for feed in self.feeds if feed.url == url), title or url)
        last_read = _read_at(state.get("fetched_at"))
        for item in items:
            hit = match_entity(context, url=item.link, title=item.title, text=item.summary)
            if hit is None:
                continue
            entity = hit.entity
            result.notes.append(f"mention of {entity['name'][:80]} ({hit.score:.2f}): {item.title[:160]}")
            if sensitive_mention(item.title, item.summary):
                when = f" {item.published_at.date().isoformat()}" if item.published_at else ""
                result.review.append({
                    "kind": "news_mention", "entity_id": entity["entity_id"], "title": item.title[:300] or f"{publisher} mentions {entity['name']}", "url": item.link,
                    "detail": f"{publisher[:80]}{when}: {item.summary[:500]}",
                })
            elif last_read is None or (item.published_at is not None and item.published_at > last_read):
                result.mentions += 1
        result.notes.append(f"{len(items)} items")
        return result


__all__ = ["FEED_TYPES", "KANNADA_SENSITIVE_TERMS", "NEWS_FEEDS", "FeedConnector", "NewsFeed", "NewsFeedConnector", "load_news_feeds", "sensitive_mention", "youtube_feed_url"]
