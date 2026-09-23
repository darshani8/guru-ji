"""The shared public-web cache in front of the paid search index.

Two institutions that map the same parent body (a Math, a trust) ask the
search index the same question; without a cache the platform pays for the
same answer once per institution. A search answer is public-web data, not
tenant data, so it is kept once for everyone (``intel_shared_cache``) and a
repeat within the TTL costs nothing. Page validators stay per institution
(a shared 304 would let one institution's reading of a page hide it from
another); only complete answers are shared here.

The engine gives every connector run its own wrapper, so ``provider_calls``
counts the calls that run really paid for and a cache hit costs 0.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..search import SearchHit

SEARCH_CACHE_TTL_SECONDS = 7 * 86400


def search_cache_key(provider: str, query: str, params: Mapping[str, Any]) -> str:
    """Provider, normalised query and parameters; domain filters are a set, so their order does not matter."""

    normalised = {name: sorted(str(item) for item in value) if isinstance(value, (list, tuple, set, frozenset)) else value for name, value in params.items()}
    return json.dumps([provider, " ".join(query.split()).casefold(), normalised], sort_keys=True, separators=(",", ":"), default=str)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _hit_payload(hit: Any) -> dict[str, Any]:
    return {"url": hit.url, "title": hit.title, "snippet": hit.snippet, "published_at": _iso(getattr(hit, "published_at", None)), "source_name": getattr(hit, "source_name", ""), "retrieved_at": _iso(getattr(hit, "retrieved_at", None))}


def _hit(item: Mapping[str, Any]) -> SearchHit | None:
    try:
        extra = {"retrieved_at": datetime.fromisoformat(item["retrieved_at"])} if item.get("retrieved_at") else {}
        return SearchHit(url=str(item["url"]), title=str(item.get("title") or ""), snippet=str(item.get("snippet") or ""), published_at=datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None, source_name=str(item.get("source_name") or ""), **extra)
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(slots=True)
class CachingSearchProvider:
    """A search provider that answers repeats from the shared cache and counts the calls it really made."""

    provider: Any
    cache: Any  # the map store (cache_get / cache_put)
    ttl_seconds: int = SEARCH_CACHE_TTL_SECONDS
    clock: Callable[[], datetime] | None = None
    provider_calls: int = 0

    @property
    def provider_name(self) -> str:
        return str(getattr(self.provider, "provider_name", "search"))

    async def search(self, query: str, **params: Any) -> tuple[SearchHit, ...]:
        key = search_cache_key(self.provider_name, query, params)
        now = self.clock().isoformat() if self.clock else None
        cached = self.cache.cache_get("search", key, now=now)
        if isinstance(cached, list):
            return tuple(hit for hit in map(_hit, cached) if hit is not None)
        self.provider_calls += 1
        hits = tuple(await self.provider.search(query, **params))
        # An empty answer is an answer too; a failed search raises and is not kept.
        self.cache.cache_put("search", key, [_hit_payload(hit) for hit in hits], self.ttl_seconds, now=now)
        return hits


def cached_search(provider: Any | None, cache: Any, *, clock: Callable[[], datetime] | None = None) -> CachingSearchProvider | None:
    """A fresh wrapper (and so a fresh call counter) around ``provider``; None stays None."""

    if provider is None:
        return None
    if isinstance(provider, CachingSearchProvider):
        provider = provider.provider
    return CachingSearchProvider(provider, cache, clock=clock)


def calls_made(search: Any, before: int | None) -> float:
    """Provider calls made since ``before`` (read from ``provider_calls``); a bare provider is always one call."""

    return 1.0 if before is None else float(getattr(search, "provider_calls", before) - before)


def calls_so_far(search: Any) -> int | None:
    return getattr(search, "provider_calls", None)


__all__ = ["SEARCH_CACHE_TTL_SECONDS", "CachingSearchProvider", "cached_search", "calls_made", "calls_so_far", "search_cache_key"]
