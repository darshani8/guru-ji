"""Open-web search providers for institution intelligence.

Unlike the allowlisted research adapter, these providers search the public web
at large. Results are still metadata only; page retrieval is a separate,
robots-aware step, and every result stays untrusted data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..web_research.http_transport import WebPayloadTooLarge, read_bounded
from .relevance import parse_published


class IntelligenceSearchUnavailable(RuntimeError):
    """Raised when the search provider cannot be used."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    published_at: datetime | None = None
    source_name: str = ""
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("search hit URL must be absolute HTTP(S)")


class IntelligenceSearchProvider(Protocol):
    provider_name: str

    async def search(self, query: str, *, max_results: int, days: int | None = None, topic: str = "general", include_domains: Sequence[str] = ()) -> tuple[SearchHit, ...]: ...


@dataclass(slots=True)
class StaticSearchProvider:
    """Fixture provider for tests and offline demos: returns hits whose text mentions a query term."""

    hits: tuple[SearchHit, ...] = ()
    provider_name: str = "static"
    calls: list[str] = field(default_factory=list)

    async def search(self, query: str, *, max_results: int, days: int | None = None, topic: str = "general", include_domains: Sequence[str] = ()) -> tuple[SearchHit, ...]:
        self.calls.append(query)
        terms = [term.strip('"').lower() for term in query.split() if len(term.strip('"')) > 2]
        matched = [hit for hit in self.hits if any(term in f"{hit.title} {hit.snippet} {hit.url}".lower() for term in terms)]
        if include_domains:
            hosts = [(urlparse(hit.url).hostname or "").lower() for hit in matched]
            matched = [hit for hit, host in zip(matched, hosts) if any(host == domain or host.endswith("." + domain) for domain in include_domains)]
        return tuple(matched[:max_results])


@dataclass(frozen=True, slots=True)
class TavilyIntelligenceSearchProvider:
    api_key: str = field(repr=False)
    endpoint: str = "https://api.tavily.com/search"
    timeout_seconds: float = 10.0
    max_response_bytes: int = 1_000_000
    exclude_domains: tuple[str, ...] = ()
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)
    provider_name: str = field(default="tavily", init=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("search API key must not be blank")
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query:
            raise ValueError("search endpoint must be a clean absolute HTTP(S) URL")

    @staticmethod
    def _text(value: object, limit: int) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""

    def _parse(self, payload: Mapping[str, Any]) -> tuple[SearchHit, ...]:
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise IntelligenceSearchUnavailable("search provider returned an invalid result list")
        hits: list[SearchHit] = []
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                continue
            url = self._text(raw.get("url"), 2000)
            title = self._text(raw.get("title"), 300) or url
            snippet = self._text(raw.get("content"), 2000)
            if not url:
                continue
            try:
                hits.append(SearchHit(url=url, title=title, snippet=snippet, published_at=parse_published(raw.get("published_date")), source_name=urlparse(url).hostname or ""))
            except ValueError:
                continue
        return tuple(hits)

    async def search(self, query: str, *, max_results: int, days: int | None = None, topic: str = "general", include_domains: Sequence[str] = ()) -> tuple[SearchHit, ...]:
        if not query.strip() or len(query) > 400:
            raise ValueError("query must be 1 to 400 characters")
        payload: dict[str, Any] = {
            "api_key": self.api_key, "query": query.strip(), "search_depth": "basic", "max_results": max(1, min(max_results, 20)),
            "include_answer": False, "include_raw_content": False, "topic": "news" if topic == "news" else "general",
        }
        if days and topic == "news":
            payload["days"] = max(1, min(int(days), 365))
        if self.exclude_domains:
            payload["exclude_domains"] = list(self.exclude_domains)
        if include_domains:
            payload["include_domains"] = [domain for domain in include_domains if domain][:20]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                async with client.stream("POST", self.endpoint, headers={"Accept": "application/json", "Content-Type": "application/json"}, json=payload) as response:
                    response.raise_for_status()
                    raw = await read_bounded(response, self.max_response_bytes)
        except httpx.TimeoutException as exc:
            raise IntelligenceSearchUnavailable("search provider timed out") from exc
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError) as exc:
            raise IntelligenceSearchUnavailable("search provider was unavailable or invalid") from exc
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise IntelligenceSearchUnavailable("search provider returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise IntelligenceSearchUnavailable("search provider returned an invalid object")
        return self._parse(decoded)


def hits_from_fixture(items: Sequence[Mapping[str, Any]]) -> tuple[SearchHit, ...]:
    return tuple(SearchHit(url=str(item["url"]), title=str(item.get("title") or item["url"]), snippet=str(item.get("snippet") or item.get("content") or ""), published_at=parse_published(item.get("published_at")), source_name=str(item.get("source_name") or "")) for item in items)


__all__ = ["IntelligenceSearchProvider", "IntelligenceSearchUnavailable", "SearchHit", "StaticSearchProvider", "TavilyIntelligenceSearchProvider", "hits_from_fixture"]
