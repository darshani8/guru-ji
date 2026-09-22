"""Robots-aware, bounded retrieval of public pages for evidence extraction.

Every request, including the robots.txt lookup and every redirect hop, goes to
a host that resolves only to public addresses; loopback, link-local, private,
reserved, multicast and unspecified addresses are refused so a page chosen by
a search provider can never pull an internal service into the evidence store.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

from ..web_research.extractor import _VisibleTextParser
from ..web_research.http_transport import WebPayloadTooLarge, read_bounded
from .relevance import parse_published
from .urls import domain_of

USER_AGENT = "GuruJi-InstitutionIntelligence/1.0 (+https://example.invalid/robots-respecting)"
DEFAULT_SNIPPET_ONLY_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "threads.net", "reddit.com", "quora.com")
MAX_REDIRECTS = 3
ROBOTS_MAX_BYTES = 200_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_META_DATE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|datePublished|date|pubdate|publish-date|dc\.date(?:\.issued)?)["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TIME_TAG = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.IGNORECASE)

HostResolver = Callable[[str], Sequence[str]]


def resolve_host(hostname: str) -> tuple[str, ...]:
    """Resolve a hostname to its addresses with the system resolver (blocking)."""

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return ()
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: str) -> bool:
    """True only for a globally routable unicast address."""

    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str  # the final URL after any redirects
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
    max_redirects: int = MAX_REDIRECTS
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    resolver: HostResolver = field(default=resolve_host, repr=False)
    _robots_cache: dict[str, robotparser.RobotFileParser | None] = field(default_factory=dict, repr=False)
    _host_cache: dict[str, bool] = field(default_factory=dict, repr=False)

    def allowed_domain(self, url: str) -> bool:
        domain = domain_of(url)
        return not any(domain == item or domain.endswith("." + item) for item in self.snippet_only_domains)

    async def _host_allowed(self, hostname: str | None) -> bool:
        """Deny unless every address the host resolves to is public."""

        host = (hostname or "").strip().lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        if host not in self._host_cache:
            try:
                addresses = tuple(await asyncio.get_running_loop().run_in_executor(None, self.resolver, host))
            except Exception:  # noqa: BLE001 - an unresolvable host is not fetched
                addresses = ()
            self._host_cache[host] = bool(addresses) and all(is_public_address(str(address)) for address in addresses)
        return self._host_cache[host]

    async def _robots_allows(self, client: httpx.AsyncClient, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                async with client.stream("GET", f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT}) as response:
                    if response.status_code == 200:
                        raw = await read_bounded(response, ROBOTS_MAX_BYTES)
                        parser.parse(raw.decode("utf-8", errors="replace").splitlines())
                    elif 400 <= response.status_code < 500:
                        parser.parse([])  # no robots file: nothing is restricted
                    else:
                        parser = None  # redirects, server errors: treat as disallowed until the site answers plainly
            except (httpx.HTTPError, WebPayloadTooLarge, ValueError):
                parser = None  # oversize or failed robots fetch: disallow
            self._robots_cache[origin] = parser
        parser = self._robots_cache[origin]
        if parser is None:
            return False
        return parser.can_fetch(USER_AGENT, url)

    async def _permitted(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or not self.allowed_domain(url):
            return False
        if not await self._host_allowed(parsed.hostname):
            return False
        return await self._robots_allows(client, url)

    async def fetch(self, url: str) -> FetchedPage | None:
        warnings: list[str] = []
        current = url
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                for _hop in range(self.max_redirects + 1):
                    if not await self._permitted(client, current):
                        return None
                    async with client.stream("GET", current, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                return None
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            return None
                        content_type = response.headers.get("content-type", "").lower()
                        if "html" not in content_type and "text/plain" not in content_type:
                            return None
                        raw = await read_bounded(response, self.max_response_bytes)
                    break
                else:
                    return None  # still redirecting after max_redirects hops
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError):
            return None
        if current != url:
            warnings.append("redirected")
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
        return FetchedPage(url=current, title=parser.title[:300], text=parser.text[:40_000], published_at=published, warnings=tuple(warnings))


__all__ = ["DEFAULT_SNIPPET_ONLY_DOMAINS", "MAX_REDIRECTS", "ROBOTS_MAX_BYTES", "FetchedPage", "PublicPageFetcher", "USER_AGENT", "is_public_address", "resolve_host"]
