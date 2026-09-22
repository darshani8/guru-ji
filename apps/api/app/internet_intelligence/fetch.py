"""Robots-aware, bounded retrieval of public pages for evidence extraction.

Every request, including the robots.txt lookup and every redirect hop, is made
to an address the fetcher resolved and vetted itself: the connection is pinned
to that address while the Host header and the TLS server name carry the
hostname. A DNS answer that changes between the check and the connection can
therefore never steer a request at loopback, link-local, private, shared,
reserved or multicast address space.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
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
# robots.txt answers are re-read after a day; a failed read (server error,
# oversize, redirect loop) disallows the site only for a short while, so one
# outage no longer shuts a site out for the life of the process.
ROBOTS_TTL_SECONDS = 86_400.0
ROBOTS_FAILURE_TTL_SECONDS = 900.0
DEFAULT_SNIPPET_ONLY_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "threads.net", "reddit.com", "quora.com")
MAX_REDIRECTS = 3
ROBOTS_MAX_BYTES = 200_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_META_DATE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|datePublished|date|pubdate|publish-date|dc\.date(?:\.issued)?)["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TIME_TAG = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.IGNORECASE)
# Ranges Python's ipaddress still reports as global but that are not public:
# shared address space (carrier-grade NAT, cloud-internal networks and
# overlays) and deprecated site-local IPv6.
_NON_PUBLIC_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fec0::/10"))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

HostResolver = Callable[[str], Sequence[str]]


def crawler_user_agent(contact: str | None) -> str:
    """The crawler's User-Agent, naming where site owners can reach the operator.

    ``contact`` is an https URL or an email address from deployment settings;
    without one the placeholder contact is used.
    """

    value = (contact or "").strip()
    if not value:
        return USER_AGENT
    if value.startswith("https://") and " " not in value and "(" not in value and ")" not in value:
        return f"GuruJi-InstitutionIntelligence/1.0 (+{value})"
    if re.fullmatch(r"[^@\s()]+@[^@\s()]+\.[a-zA-Z]{2,}", value):
        return f"GuruJi-InstitutionIntelligence/1.0 (+mailto:{value})"
    raise ValueError("the crawler contact must be an https URL or an email address")


def resolve_host(hostname: str) -> tuple[str, ...]:
    """Resolve a hostname to its addresses with the system resolver (blocking)."""

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return ()
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: str) -> bool:
    """True only for a globally routable unicast address.

    IPv6 addresses that embed an IPv4 address (mapped, 6to4, Teredo, NAT64)
    are judged by the embedded address, so an internal IPv4 service cannot
    hide behind an IPv6 spelling.
    """

    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(str(embedded))
    if any(ip in network for network in _NON_PUBLIC_NETWORKS):
        return False
    return bool(ip.is_global) and not ip.is_multicast


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str  # the final URL after any redirects
    title: str
    text: str
    published_at: datetime | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Target:
    """A vetted request: the logical URL plus the pinned address it is sent to."""

    url: str
    pinned_url: str
    host_header: str
    sni_hostname: str | None
    user_agent: str = USER_AGENT

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Host": self.host_header}

    @property
    def extensions(self) -> dict[str, str]:
        return {"sni_hostname": self.sni_hostname} if self.sni_hostname else {}


@dataclass(slots=True)
class PublicPageFetcher:
    timeout_seconds: float = 8.0
    max_response_bytes: int = 1_000_000
    snippet_only_domains: tuple[str, ...] = DEFAULT_SNIPPET_ONLY_DOMAINS
    respect_robots: bool = True
    max_redirects: int = MAX_REDIRECTS
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    resolver: HostResolver = field(default=resolve_host, repr=False)
    user_agent: str = USER_AGENT
    robots_ttl_seconds: float = ROBOTS_TTL_SECONDS
    robots_failure_ttl_seconds: float = ROBOTS_FAILURE_TTL_SECONDS
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _robots_cache: dict[str, tuple[float, robotparser.RobotFileParser | None]] = field(default_factory=dict, repr=False)

    def allowed_domain(self, url: str) -> bool:
        domain = domain_of(url)
        return not any(domain == item or domain.endswith("." + item) for item in self.snippet_only_domains)

    async def _public_address(self, hostname: str | None) -> str | None:
        """The address to connect to, or None unless every address the host resolves to is public."""

        host = (hostname or "").strip().lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost"):
            return None
        try:
            # Resolution is bounded like the connection used to be, so a black-holed
            # nameserver cannot stall an investigation for its full retry schedule.
            addresses = tuple(await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, self.resolver, host), timeout=self.timeout_seconds))
        except Exception:  # noqa: BLE001 - unresolvable, refused or slow hosts are not fetched
            return None
        if not addresses or not all(is_public_address(str(address)) for address in addresses):
            return None
        return str(addresses[0])

    def _pin(self, url: str, address: str) -> _Target | None:
        """Bind a URL to the address it will be sent to; the Host header and TLS name keep the hostname."""

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        literal = f"[{address}]" if ":" in address else address
        netloc = literal if port is None else f"{literal}:{port}"
        host_header = parsed.hostname if port is None else f"{parsed.hostname}:{port}"
        return _Target(url=url, pinned_url=parsed._replace(netloc=netloc).geturl(), host_header=host_header, sni_hostname=parsed.hostname if parsed.scheme == "https" else None, user_agent=self.user_agent)

    async def _vet(self, url: str, *, check_domain: bool = True) -> _Target | None:
        """Resolve and check one URL; the returned target is pinned to the vetted address."""

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or not parsed.hostname:
            return None
        if check_domain and not self.allowed_domain(url):
            return None
        address = await self._public_address(parsed.hostname)
        if address is None:
            return None
        return self._pin(url, address)

    async def _load_robots(self, client: httpx.AsyncClient, origin: str, address: str) -> robotparser.RobotFileParser | None:
        """Fetch and parse an origin's robots.txt; None means the site is not fetched.

        The first request reuses the address vetted for the page, so one
        lookup covers both. Redirects (apex to www, http to https) are followed
        with the same checks as page requests. A missing file restricts
        nothing; an oversize, failed or unreadable one disallows everything.
        """

        parser = robotparser.RobotFileParser()
        current = f"{origin}/robots.txt"
        try:
            for hop in range(self.max_redirects + 1):
                target = self._pin(current, address) if hop == 0 else await self._vet(current, check_domain=False)
                if target is None:
                    return None
                async with client.stream("GET", target.pinned_url, headers=target.headers, extensions=target.extensions) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if response.status_code == 200:
                        raw = await read_bounded(response, ROBOTS_MAX_BYTES)
                        parser.parse(raw.decode("utf-8", errors="replace").splitlines())
                        return parser
                    if 400 <= response.status_code < 500:
                        parser.parse([])  # no robots file: nothing is restricted
                        return parser
                    return None  # server errors: disallow until the site answers plainly
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError):
            return None
        return None  # still redirecting after max_redirects hops

    async def _robots_allows(self, client: httpx.AsyncClient, target: _Target) -> bool:
        if not self.respect_robots:
            return True
        url = target.url
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = self.clock()
        cached = self._robots_cache.get(origin)
        if cached is None or cached[0] <= now:
            loaded = await self._load_robots(client, origin, urlparse(target.pinned_url).hostname or "")
            ttl = self.robots_ttl_seconds if loaded is not None else self.robots_failure_ttl_seconds
            cached = (now + ttl, loaded)
            self._robots_cache[origin] = cached
        parser = cached[1]
        if parser is None:
            return False
        return parser.can_fetch(self.user_agent, url)

    async def fetch(self, url: str) -> FetchedPage | None:
        warnings: list[str] = []
        current = url
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                for _hop in range(self.max_redirects + 1):
                    target = await self._vet(current)
                    if target is None or not await self._robots_allows(client, target):
                        return None
                    headers = {**target.headers, "Accept": "text/html,application/xhtml+xml"}
                    async with client.stream("GET", target.pinned_url, headers=headers, extensions=target.extensions) as response:
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


__all__ = ["DEFAULT_SNIPPET_ONLY_DOMAINS", "MAX_REDIRECTS", "ROBOTS_FAILURE_TTL_SECONDS", "ROBOTS_MAX_BYTES", "ROBOTS_TTL_SECONDS", "FetchedPage", "PublicPageFetcher", "USER_AGENT", "crawler_user_agent", "is_public_address", "resolve_host"]
