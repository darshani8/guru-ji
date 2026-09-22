"""Robots-aware, bounded retrieval of public pages for evidence extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from ..web_research.extractor import _VisibleTextParser
from ..web_research.http_transport import WebPayloadTooLarge, read_bounded
from .relevance import parse_published
from .urls import domain_of

USER_AGENT = "GuruJi-InstitutionIntelligence/1.0 (+https://example.invalid/robots-respecting)"
DEFAULT_SNIPPET_ONLY_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "threads.net", "reddit.com", "quora.com")
_META_DATE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|datePublished|date|pubdate|publish-date|dc\.date(?:\.issued)?)["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TIME_TAG = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    title: str
    text: str
    published_at: datetime | None
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class PublicPageFetcher:
    timeout_seconds: float = 8.0
    max_response_bytes: int = 1_000_000
    snippet_only_domains: tuple[str, ...] = DEFAULT_SNIPPET_ONLY_DOMAINS
    respect_robots: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    _robots_cache: dict[str, robotparser.RobotFileParser | None] = field(default_factory=dict, repr=False)

    def allowed_domain(self, url: str) -> bool:
        domain = domain_of(url)
        return not any(domain == item or domain.endswith("." + item) for item in self.snippet_only_domains)

    async def _robots_allows(self, client: httpx.AsyncClient, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                response = await client.get(f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT})
                if response.status_code == 200 and len(response.content) < 200_000:
                    parser.parse(response.text.splitlines())
                elif response.status_code >= 500:
                    parser = None  # treat as disallowed until the site recovers
                else:
                    parser.parse([])
            except httpx.HTTPError:
                parser = None
            self._robots_cache[origin] = parser
        parser = self._robots_cache[origin]
        if parser is None:
            return False
        return parser.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> FetchedPage | None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not self.allowed_domain(url):
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=True, max_redirects=3) as client:
                if not await self._robots_allows(client, url):
                    return None
                async with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}) as response:
                    if response.status_code != 200:
                        return None
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type and "text/plain" not in content_type:
                        return None
                    raw = await read_bounded(response, self.max_response_bytes)
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError):
            return None
        html = raw.decode("utf-8", errors="replace")
        parser = _VisibleTextParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001 - tolerate broken markup
            return None
        published = None
        for pattern in (_META_DATE, _TIME_TAG):
            match = pattern.search(html)
            if match:
                published = parse_published(match.group(1))
                if published:
                    break
        return FetchedPage(url=str(url), title=parser.title[:300], text=parser.text[:40_000], published_at=published)


__all__ = ["DEFAULT_SNIPPET_ONLY_DOMAINS", "FetchedPage", "PublicPageFetcher", "USER_AGENT"]
