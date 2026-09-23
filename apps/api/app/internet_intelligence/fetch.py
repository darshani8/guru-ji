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
import contextlib
import ipaddress
import re
import socket
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
HTML_TYPES = frozenset({"html", "text"})
_CONTENT_FAMILIES = {
    "html": ("text/html", "application/xhtml+xml"), "text": ("text/plain",), "feed": ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"),
    "xml": ("application/xml", "text/xml"), "json": ("application/json", "application/ld+json", "application/sparql-results+json", "text/json"), "pdf": ("application/pdf",),
}
_LOGIN_PATH = re.compile(r"/(?:accounts/)?log[-_]?in\b|/authwall|/signin\b|/checkpoint\b", re.IGNORECASE)
# The "/1.0" of a robots.txt User-agent line: groups match on the product token alone.
_ROBOTS_AGENT_VERSION = re.compile(r"^(\s*user-agent\s*:\s*[^/\s#]+)/[^\s#]*", re.IGNORECASE | re.MULTILINE)


# Every host map/assets.py reads as a social platform, short links included (fb.me, youtu.be, t.me, wa.me),
# since a redirect from any page can land on one. Link hubs (linktr.ee, bio.link) stay fetchable on
# purpose: the link-hub connector reads them.
DEFAULT_SNIPPET_ONLY_DOMAINS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "youtu.be", "threads.net", "threads.com", "t.me", "telegram.me", "wa.me", "wa.link",
    "whatsapp.com", "pinterest.com", "snapchat.com", "sharechat.com", "reddit.com", "redd.it", "quora.com", "justdial.com", "glassdoor.com",
)
# Sites whose pages are never fetched under any country domain (glassdoor.co.in,
# justdial.co.in ...): search snippets about them are all the platform uses.
NEVER_FETCHED_SITES = ("glassdoor", "justdial", "quora", "reddit")
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
# One request at a time per host, at least this far apart; a site's robots.txt
# Crawl-delay stretches the gap, but never past a minute.
MIN_HOST_INTERVAL_SECONDS = 1.0
MAX_CRAWL_DELAY_SECONDS = 60.0
# httpx's timeout bounds each socket operation, not the request: a site that drips a byte just inside it
# would hold the host (and the job waiting on it) for days. One request, body included, gets this many
# times ``timeout_seconds`` in all, unless the fetcher is given its own ``deadline_seconds``.
DEADLINE_TIMEOUTS = 3.0
# getaddrinfo's answers for a name that does not exist or has no address; any
# other resolver failure (a timeout, SERVFAIL) is an outage, not a lapsed name.
_NO_SUCH_NAME = frozenset(code for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None), getattr(socket, "EAI_ADDRFAMILY", None)) if code is not None)

HostResolver = Callable[[str], Sequence[str]]
# (host, seconds to hold it) -> seconds to wait before trying to claim the host again; 0 means claimed.
HostSlots = Callable[[str, float], float]
# (host, seconds until the next request may start): hands a claimed host back once its request is over.
HostRelease = Callable[[str, float], None]


class _Unfetchable(ValueError):
    """A host that cannot be fetched for a reason worth reporting (it does not resolve)."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


class _HostBusy(Exception):
    """Another worker kept the host for longer than the fetcher waits: the request is not made.

    Not a ValueError like ``_Unfetchable``, so a robots.txt lookup never
    swallows it and caches the site as disallowed; ``retrieve`` says "busy".
    """


def _refused(error: BaseException) -> bool:
    """Whether a connection failure was the host refusing it (nothing listens there any more)."""

    seen: BaseException | None = error
    for _ in range(6):
        if seen is None:
            return False
        if isinstance(seen, ConnectionRefusedError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _never_fetched(domain: str) -> bool:
    """Whether ``domain`` is one of NEVER_FETCHED_SITES under any country domain ("www.glassdoor.co.in")."""

    labels = domain.lower().rstrip(".").split(".")
    for index, label in enumerate(labels[:-1]):
        # The site's name followed only by a public suffix ("com", "co.in", "com.au").
        if label in NEVER_FETCHED_SITES and all(len(rest) <= 3 for rest in labels[index + 1 :]):
            return True
    return False


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


def product_token(user_agent: str) -> str:
    """The name robots.txt groups are matched on: "GuruJi-InstitutionIntelligence", without version or contact."""

    return user_agent.split("/", 1)[0].split(" ", 1)[0].strip()


def robots_lines(text: str) -> list[str]:
    """robots.txt lines with any version dropped from User-agent names.

    urllib's parser compares a group's name with the product token only, so
    a group written for "GuruJi-InstitutionIntelligence/1.0" (the full
    User-Agent a webmaster sees in their logs) would otherwise never match
    and its rules would be silently ignored.
    """

    return _ROBOTS_AGENT_VERSION.sub(r"\1", text).splitlines()


def resolve_host(hostname: str) -> tuple[str, ...]:
    """Resolve a hostname to its addresses with the system resolver (blocking).

    A name that does not exist or has no address gives no addresses; any
    other resolver failure raises, so a DNS outage never reads as a lapsed name.
    """

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except UnicodeError:
        return ()
    except socket.gaierror as exc:
        if exc.errno in _NO_SUCH_NAME:
            return ()
        raise
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
    min_host_interval: float = MIN_HOST_INTERVAL_SECONDS
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    host_slots: HostSlots | None = field(default=None, repr=False)  # spaces requests across processes too (the map store's shared table)
    host_release: HostRelease | None = field(default=None, repr=False)  # hands a claimed host back early; without it the claim runs out on its own
    deadline_seconds: float | None = None  # the most one request may take in all; DEADLINE_TIMEOUTS x timeout_seconds when unset
    _robots_cache: dict[str, tuple[float, robotparser.RobotFileParser | None]] = field(default_factory=dict, repr=False)
    _host_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = field(default_factory=weakref.WeakKeyDictionary, repr=False)
    _host_last: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def deadline(self) -> float:
        """Seconds one request may take, from sending it to the last byte of its body."""

        return self.deadline_seconds if self.deadline_seconds is not None else DEADLINE_TIMEOUTS * self.timeout_seconds

    def allowed_domain(self, url: str) -> bool:
        # "www.facebook.com." names the same host as "www.facebook.com": the root's dot must not slip it past the list.
        domain = domain_of(url).rstrip(".")
        if any(domain == item or domain.endswith("." + item) for item in self.snippet_only_domains):
            return False
        return not _never_fetched(domain)

    def sitemaps(self, url: str) -> tuple[str, ...]:
        """The sitemaps the robots.txt already read for ``url``'s origin declares (none before it is read)."""

        parsed = urlparse(url)
        cached = self._robots_cache.get(f"{parsed.scheme}://{parsed.netloc}")
        return tuple((cached[1].site_maps() or [])[:5]) if cached is not None and cached[1] is not None else ()

    @contextlib.asynccontextmanager
    async def _turn(self, url: str) -> AsyncIterator[None]:
        """Hold ``url``'s host for one request, after waiting out the gap since the last one.

        The gap is the larger of ``min_host_interval`` and the site's own
        Crawl-delay (at most a minute), counted from when the last request
        finished. The host's lock is held through the request, so this process
        never has two requests in flight to one host; ``host_slots`` extends
        that to every process that shares it. Its claim is a lease on the host
        for the gap plus the whole request (``deadline``, which the request
        cannot outlast), handed back through ``host_release`` when the request
        ends so the next may start a gap later. A host another worker keeps
        past the wait is not requested at all: ``_HostBusy``.
        """

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        # A lock is bound to the loop it first waited in, and a fetcher outlives each job's asyncio.run().
        lock = self._host_locks.setdefault(asyncio.get_running_loop(), {}).setdefault(host, asyncio.Lock())
        async with lock:
            cached = self._robots_cache.get(f"{parsed.scheme}://{parsed.netloc}")
            delay = (cached[1].crawl_delay(product_token(self.user_agent)) if cached is not None and cached[1] is not None else None) or 0
            interval = max(self.min_host_interval, min(float(delay), MAX_CRAWL_DELAY_SECONDS))
            wait = self._host_last.get(host, float("-inf")) + interval - self.clock()
            if wait > 0:
                await self.sleep(wait)
            lease, waited = interval + self.deadline, 0.0
            # Another process holds the host: ask again at least once a gap (it may hand the host back
            # before its lease runs out), for a bounded time; past that the request is not made.
            while self.host_slots is not None and (wait := float(self.host_slots(host, lease))) > 0:
                if waited >= MAX_CRAWL_DELAY_SECONDS + lease:
                    raise _HostBusy(host)
                wait = min(max(wait, 0.05), max(interval, 1.0))
                await self.sleep(wait)
                waited += wait
            try:
                yield
            finally:
                self._host_last[host] = self.clock()
                if self.host_slots is not None and self.host_release is not None:
                    with contextlib.suppress(Exception):  # a failed hand-back only keeps the host until its lease runs out
                        self.host_release(host, interval)

    async def _public_address(self, hostname: str | None) -> str | None:
        """The address to connect to, or None unless every address the host resolves to is public.

        A name that no longer resolves raises ``_Unfetchable("unresolved")``:
        that is how a lapsed domain dies, and it must not read as a private one.
        """

        host = (hostname or "").strip().lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost"):
            return None
        try:
            # Resolution is bounded like the connection used to be, so a black-holed
            # nameserver cannot stall an investigation for its full retry schedule.
            addresses = tuple(await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, self.resolver, host), timeout=self.timeout_seconds))
        except TimeoutError:
            raise _Unfetchable("timeout") from None
        except Exception:  # noqa: BLE001 - a failing resolver (SERVFAIL, refused) is an outage, not a verdict on the name
            raise _Unfetchable("error") from None
        if not addresses:
            raise _Unfetchable("unresolved")
        if not all(is_public_address(str(address)) for address in addresses):
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
        with the same checks as page requests, the domain list included: a
        robots.txt sent on to a social or never-fetched site is a failed read,
        never a request there. A missing file restricts nothing; an oversize,
        failed or unreadable one disallows everything; one that outlasts the
        deadline raises TimeoutError for ``retrieve`` to report.
        """

        parser = robotparser.RobotFileParser()
        current = f"{origin}/robots.txt"
        try:
            for hop in range(self.max_redirects + 1):
                target = self._pin(current, address) if hop == 0 else await self._vet(current)
                if target is None:
                    return None
                async with self._turn(target.url), asyncio.timeout(self.deadline), client.stream("GET", target.pinned_url, headers=target.headers, extensions=target.extensions) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if response.status_code == 200:
                        raw = await read_bounded(response, ROBOTS_MAX_BYTES)
                        parser.parse(robots_lines(raw.decode("utf-8", errors="replace")))
                        return parser
                    if 400 <= response.status_code < 500:
                        parser.parse([])  # no robots file: nothing is restricted
                        return parser
                    return None  # server errors: disallow until the site answers plainly
        except httpx.ConnectError as exc:
            if hop == 0 and _refused(exc):
                raise  # the site's own host refused: the page would be too, and ``retrieve`` says so
            return None
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
            try:
                loaded = await self._load_robots(client, origin, urlparse(target.pinned_url).hostname or "")
            except TimeoutError:
                # Past the deadline is a failed read like any other; only this retrieval says it timed out.
                self._robots_cache[origin] = (now + self.robots_failure_ttl_seconds, None)
                raise
            ttl = self.robots_ttl_seconds if loaded is not None else self.robots_failure_ttl_seconds
            cached = (now + ttl, loaded)
            self._robots_cache[origin] = cached
        parser = cached[1]
        if parser is None:
            return False
        return parser.can_fetch(product_token(self.user_agent), url)

    async def retrieve(
        self, url: str, *, accept: frozenset[str] = HTML_TYPES, etag: str | None = None, last_modified: str | None = None, max_bytes: int | None = None, check_domain: bool = True,
    ) -> Retrieval:
        """Fetch one URL and say exactly what happened.

        The same vetted, pinned, robots-aware path as ``fetch``, but the
        outcome is reported instead of collapsed into None: robots refusal,
        a block (403/429/999), a login wall, not found, a server error, a
        timeout, a disallowed content type, or ``not_modified`` when the
        validators from the last fetch still hold, or ``busy`` when another
        worker kept the host past the wait. ``accept`` names the content
        families wanted (html, text, feed, xml, json, pdf). Each request, body
        included, must be over within ``deadline`` or it is a timeout.
        """

        warnings: list[str] = []
        current = url
        limit = max_bytes or self.max_response_bytes
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                for _hop in range(self.max_redirects + 1):
                    if check_domain and not self.allowed_domain(current):
                        return Retrieval(url=current, outcome="snippet_only")
                    target = await self._vet(current, check_domain=False)
                    if target is None:
                        return Retrieval(url=current, outcome="not_public")
                    if not await self._robots_allows(client, target):
                        return Retrieval(url=current, outcome="robots")
                    headers = {**target.headers, "Accept": _accept_header(accept)}
                    if etag:
                        headers["If-None-Match"] = etag
                    if last_modified:
                        headers["If-Modified-Since"] = last_modified
                    async with self._turn(target.url), asyncio.timeout(self.deadline), client.stream("GET", target.pinned_url, headers=headers, extensions=target.extensions) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                return Retrieval(url=current, outcome="error", http_status=response.status_code)
                            following = urljoin(current, location)
                            if _LOGIN_PATH.search(urlparse(following).path):
                                return Retrieval(url=following, outcome="login_wall", http_status=response.status_code)
                            current = following
                            continue
                        status = response.status_code
                        if status == 304:
                            return Retrieval(url=current, outcome="not_modified", http_status=304, etag=response.headers.get("etag") or etag, last_modified=response.headers.get("last-modified") or last_modified)
                        if status != 200:
                            return Retrieval(url=current, outcome=_outcome_for(status), http_status=status)
                        content_type = response.headers.get("content-type", "").lower()
                        if not _content_allowed(content_type, accept):
                            return Retrieval(url=current, outcome="content_type", http_status=status, content_type=content_type)
                        raw = await read_bounded(response, limit)
                        response_etag, response_modified = response.headers.get("etag"), response.headers.get("last-modified")
                    break
                else:
                    return Retrieval(url=current, outcome="redirect_loop")
        except WebPayloadTooLarge:
            return Retrieval(url=current, outcome="too_large")
        except (httpx.TimeoutException, TimeoutError):
            return Retrieval(url=current, outcome="timeout")
        except _HostBusy:
            return Retrieval(url=current, outcome="busy")
        except _Unfetchable as exc:
            return Retrieval(url=current, outcome=exc.outcome)
        except httpx.ConnectError as exc:
            return Retrieval(url=current, outcome="unreachable" if _refused(exc) else "error")
        except (httpx.HTTPError, ValueError):
            return Retrieval(url=current, outcome="error")
        if current != url:
            warnings.append("redirected")
        if _LOGIN_PATH.search(urlparse(current).path):
            return Retrieval(url=current, outcome="login_wall", http_status=200)
        return Retrieval(url=current, outcome="ok", http_status=200, content_type=content_type, body=raw, etag=response_etag, last_modified=response_modified, warnings=tuple(warnings))

    async def fetch(self, url: str) -> FetchedPage | None:
        retrieval = await self.retrieve(url, accept=frozenset({"html", "text"}))
        if retrieval.outcome != "ok":
            return None
        html = retrieval.body.decode("utf-8", errors="replace")
        parser = _VisibleTextParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001 - tolerate broken markup
            return None
        return FetchedPage(url=retrieval.url, title=parser.title[:300], text=parser.text[:40_000], published_at=published_from_html(html), warnings=retrieval.warnings)


def published_from_html(html: str) -> datetime | None:
    for pattern in (_META_DATE, _TIME_TAG):
        match = pattern.search(html)
        if match:
            published = parse_published(match.group(1))
            if published:
                return published
    return None


def _accept_header(accept: frozenset[str]) -> str:
    types = [media for family in sorted(accept) for media in _CONTENT_FAMILIES.get(family, ())]
    return ",".join(types) or "text/html"


def _content_allowed(content_type: str, accept: frozenset[str]) -> bool:
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip()
    if "html" in accept and "html" in media:
        return True
    return any(media == allowed for family in accept for allowed in _CONTENT_FAMILIES.get(family, ()))


def _outcome_for(status: int) -> str:
    if status in {401, 403, 429, 999}:
        return "blocked"
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status >= 500:
        return "server_error"
    return "error"


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What happened when a URL was fetched; ``body`` is set only when ``outcome`` is ok."""

    url: str
    outcome: str  # ok | not_modified | robots | blocked | login_wall | not_found | gone | server_error | timeout | busy | content_type | too_large | not_public | unresolved | unreachable | snippet_only | redirect_loop | error
    http_status: int | None = None
    content_type: str = ""
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


__all__ = ["DEADLINE_TIMEOUTS", "DEFAULT_SNIPPET_ONLY_DOMAINS", "FetchedPage", "HTML_TYPES", "MAX_CRAWL_DELAY_SECONDS", "MAX_REDIRECTS", "MIN_HOST_INTERVAL_SECONDS", "NEVER_FETCHED_SITES", "PublicPageFetcher", "ROBOTS_FAILURE_TTL_SECONDS", "ROBOTS_MAX_BYTES", "ROBOTS_TTL_SECONDS", "Retrieval", "USER_AGENT", "crawler_user_agent", "is_public_address", "product_token", "published_from_html", "resolve_host", "robots_lines"]
