"""Provider-neutral public-web search contracts and a bounded Tavily adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .http_transport import WebPayloadTooLarge, read_bounded


class WebSearchError(RuntimeError):
    """Base class for safe public-web search failures."""


class WebSearchUnavailable(WebSearchError):
    """Raised when the configured search provider cannot be used safely."""


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    url: str
    title: str
    excerpt: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("web search result URL must be an absolute HTTP(S) URL")
        if not self.title.strip() or not self.excerpt.strip():
            raise ValueError("web search result title and excerpt must not be blank")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")


class WebSearchProvider(Protocol):
    provider_name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        allowed_domains: frozenset[str],
    ) -> tuple[WebSearchResult, ...]:
        """Return bounded search results without executing page instructions."""


@dataclass(frozen=True, slots=True)
class TavilyHttpSearchProvider:
    """Minimal Tavily-compatible adapter using the repository's httpx runtime.

    The provider only returns search metadata. Page retrieval is a separate,
    allowlisted operation so search results cannot silently broaden the trust
    boundary.
    """

    api_key: str = field(repr=False)
    endpoint: str = "https://api.tavily.com/search"
    timeout_seconds: float = 8.0
    max_response_bytes: int = 512_000
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)
    provider_name: str = field(default="tavily", init=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("web search API key must not be blank")
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("web search endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("web search endpoint must not contain credentials, query, or fragment data")
        if self.timeout_seconds <= 0:
            raise ValueError("web search timeout must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("web search max response bytes must be positive")

    @staticmethod
    def _text(value: object, *, limit: int) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""

    @classmethod
    def _parse_results(cls, value: Mapping[str, Any], retrieved_at: datetime) -> tuple[WebSearchResult, ...]:
        raw_results = value.get("results", [])
        if not isinstance(raw_results, list):
            raise WebSearchUnavailable("search provider returned an invalid result list")
        results: list[WebSearchResult] = []
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                continue
            url = cls._text(raw.get("url"), limit=2_000)
            title = cls._text(raw.get("title"), limit=300) or url
            excerpt = cls._text(raw.get("content"), limit=1_500)
            if not url or not excerpt:
                continue
            try:
                results.append(WebSearchResult(url=url, title=title, excerpt=excerpt, retrieved_at=retrieved_at))
            except ValueError:
                continue
        return tuple(results)

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        allowed_domains: frozenset[str],
    ) -> tuple[WebSearchResult, ...]:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 500:
            raise ValueError("web research query must contain 1 to 500 characters")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        if not allowed_domains:
            raise ValueError("web research requires at least one allowed domain")

        payload = {
            "api_key": self.api_key,
            "query": normalized_query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_domains": sorted(allowed_domains),
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    raw = await read_bounded(response, self.max_response_bytes)
        except httpx.TimeoutException as exc:
            raise WebSearchUnavailable("public-web search provider timed out") from exc
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError) as exc:
            raise WebSearchUnavailable("public-web search provider was unavailable or invalid") from exc

        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise WebSearchUnavailable("public-web search provider returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise WebSearchUnavailable("public-web search provider returned an invalid object")
        return self._parse_results(decoded, datetime.now(timezone.utc))


__all__ = [
    "TavilyHttpSearchProvider",
    "WebSearchError",
    "WebSearchProvider",
    "WebSearchResult",
    "WebSearchUnavailable",
]
