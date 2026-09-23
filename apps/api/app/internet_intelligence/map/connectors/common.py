"""What the connectors share: a bounded client for fixed API endpoints, entity matching and feed parsing."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlsplit

import httpx

from ...entity_resolution import HIGH, MEDIUM, resolve_entity
from ...fetch import USER_AGENT
from ...profile import InstitutionProfile
from ...relevance import parse_published
from ....web_research.http_transport import WebPayloadTooLarge, read_bounded
from .base import ConnectorContext


@dataclass(frozen=True, slots=True)
class ApiResponse:
    outcome: str  # ok | not_found | blocked | server_error | timeout | too_large | error
    status: int = 0
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _outcome(status: int) -> str:
    if status == 200:
        return "ok"
    if status in {404, 410}:
        return "not_found"
    if status in {401, 403, 429}:
        return "blocked"
    if status >= 500:
        return "server_error"
    return "error"


# Parameters and headers that carry a credential: never part of a cache key,
# and a request that sends a credential header is not shared at all.
SECRET_PARAMS = frozenset({"key", "token", "api_key", "apikey", "access_token"})
_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


def api_cache_key(url: str, params: Mapping[str, Any] | None = None, *, method: str = "GET", headers: Mapping[str, str] | None = None, body: bool = False) -> str | None:
    """URL and sorted parameters without any key or token; None for a request that must not be shared."""

    parts = urlsplit(url)
    if method.upper() != "GET" or body or parts.username or parts.password or any(name.lower() in _CREDENTIAL_HEADERS or "token" in name.lower() or "key" in name.lower() for name in headers or {}):
        return None
    pairs = parse_qsl(parts.query, keep_blank_values=True) + [(str(name), str(value)) for name, value in (params or {}).items()]
    return json.dumps([f"{parts.scheme}://{parts.netloc}{parts.path}", sorted((name, value) for name, value in pairs if name.lower() not in SECRET_PARAMS)], separators=(",", ":"))


def _shareable(content_type: str, body: bytes) -> bool:
    """Only a JSON or XML answer that is valid UTF-8 text goes into the shared cache."""

    if not any(kind in content_type.lower() for kind in ("json", "xml")):
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass(slots=True)
class ApiClient:
    """Calls documented public APIs at fixed HTTPS endpoints (not pages, so no robots.txt).

    A refusal (401/403/429) is reported as ``blocked`` and never retried here;
    the engine backs the source off. Redirects are followed only to HTTPS.
    """

    user_agent: str = USER_AGENT
    timeout_seconds: float = 10.0
    max_bytes: int = 2_000_000
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    # The shared public-web cache (the map store), for open APIs only: a 200
    # JSON or XML answer to a GET is kept for every institution.
    cache: Any | None = field(default=None, repr=False)
    cache_ttl_seconds: int = 86400

    async def request(self, url: str, *, method: str = "GET", params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None, json_body: Any = None, data: Mapping[str, Any] | None = None) -> ApiResponse:
        if urlparse(url).scheme != "https":
            raise ValueError("API endpoints must be HTTPS")
        cache_key = api_cache_key(url, params, method=method, headers=headers, body=json_body is not None or data is not None) if self.cache is not None else None
        cached = self.cache.cache_get("api", cache_key) if cache_key else None
        if isinstance(cached, dict) and isinstance(cached.get("body"), str):
            return ApiResponse("ok", 200, cached["body"].encode("utf-8"))
        current = url
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                for _ in range(4):
                    async with client.stream(method, current, params=params, headers={"User-Agent": self.user_agent, "Accept": "application/json, application/xml;q=0.9, */*;q=0.5", **(headers or {})}, json=json_body, data=data) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "")
                            following = str(response.url.join(location)) if location else ""
                            if urlparse(following).scheme != "https":
                                return ApiResponse("error", response.status_code)
                            current, params = following, None
                            continue
                        if response.status_code != 200:
                            return ApiResponse(_outcome(response.status_code), response.status_code)
                        body = await read_bounded(response, self.max_bytes)
                        if cache_key and _shareable(response.headers.get("content-type", ""), body):
                            self.cache.cache_put("api", cache_key, {"body": body.decode("utf-8")}, self.cache_ttl_seconds)
                        return ApiResponse("ok", 200, body)
        except WebPayloadTooLarge:
            return ApiResponse("too_large")
        except httpx.TimeoutException:
            return ApiResponse("timeout")
        except (httpx.HTTPError, ValueError):
            return ApiResponse("error")
        return ApiResponse("error")


# "busy" (another worker kept the host) says nothing about the page, but the source still backs off.
FAILED_OUTCOMES = frozenset({"blocked", "server_error", "timeout", "busy", "too_large", "error", "robots", "login_wall", "not_public", "unresolved", "unreachable", "content_type", "redirect_loop"})


# ----------------------------------------------------------------- entities
# The letters a name is made of: Latin, digits and the Kannada block
# (U+0C80-U+0CFF). Not \w: Python's \w stops at Kannada vowel signs and
# viramas, so it would cut one Kannada word into pieces.
_NAME_CHARS = "a-z0-9ಀ-೿"


def _compact(text: str) -> str:
    return re.sub(f"[^{_NAME_CHARS}]", "", text.lower())


def entity_profile(entity: Mapping[str, Any], institution_id: str, text: str = "") -> InstitutionProfile | None:
    """The entity as a resolver profile, placed at whichever of its locations ``text`` names.

    The resolver weighs a single location, and an entity has several (a
    city and its Kannada spelling, a campus and its town): the first one the
    page mentions is used, so "ಬೆಂಗಳೂರು" places a Kannada article as surely
    as "Bengaluru" places an English one. Without text, the first location.
    """

    locations = [str(item) for item in entity.get("locations") or [] if str(item).strip()]
    lowered = text.lower()[:20_000]
    location = next((item for item in locations if item.lower() in lowered), locations[0] if locations else "")
    try:
        return InstitutionProfile(institution_id, str(entity["name"]), location, aliases=entity.get("names") or [])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class EntityHit:
    entity: dict[str, Any]
    score: float
    rival: float


def match_entity(context: ConnectorContext, *, url: str, title: str, text: str, margin: float = 0.2, entity_id: str | None = None, close: list[EntityHit] | None = None) -> EntityHit | None:
    """The mapped entity a page or result is about, if it names one clearly more strongly than any look-alike.

    A close call (one of ours matched, but a look-alike within ``margin``)
    returns None and, when ``close`` is given, is appended to it so the
    caller can send it to a person instead of dropping it.
    """

    best: tuple[dict[str, Any], float] | None = None
    rival = 0.0
    for entity in context.entities():
        profile = entity_profile(entity, context.institution_id, f"{title} {text}")
        if profile is None:
            continue
        match = resolve_entity(profile, url=url, title=title, text=text)
        if entity["kind"] == "lookalike":
            rival = max(rival, match.score)
        elif (entity_id is None or entity["entity_id"] == entity_id) and match.level in {HIGH, MEDIUM} and (best is None or match.score > best[1]):
            best = (entity, match.score)
    if best is None:
        return None
    if best[1] < rival + margin:
        if close is not None:
            close.append(EntityHit(best[0], best[1], rival))
        return None
    return EntityHit(best[0], best[1], rival)


# Words an institutional account puts next to its name ("bgscet_cse",
# "BGSCET Official", "bgscetalumni"); a person's name is not among them.
INSTITUTIONAL_WORDS = frozenset({
    "official", "officials", "offl", "college", "colleges", "coll", "clg", "engg", "engineering", "institute", "institution", "inst", "university", "univ", "school",
    "campus", "dept", "department", "cse", "ece", "eee", "ise", "mech", "mechanical", "civil", "it", "ai", "aiml", "ds", "mba", "mca", "bba", "bca", "bcom", "bsc", "msc",
    "mtech", "btech", "phd", "pu", "puc", "nss", "ncc", "iste", "ieee", "csi", "acm", "sae", "ecell", "alumni", "association", "placements", "placement", "tpo",
    "admissions", "admission", "library", "sports", "fest", "club", "clubs", "students", "student", "council", "union", "hostel", "events", "news", "media", "page",
    "team", "research", "innovation", "cell", "hub", "links", "community", "india", "online", "live", "tv", "channel", "updates", "group", "trust", "math", "mutt", "hospital",
    "medical", "nursing", "pharmacy", "law", "arts", "science", "commerce", "high", "primary", "english", "public", "international", "residential", "the", "of", "and", "for",
    "at", "ac", "edu", "org", "com", "net", "in", "co", "gov", "res", "www", "company", "school", "showcase", "u", "r", "c", "user",
})


def _words(text: str) -> list[str]:
    return re.findall(f"[{_NAME_CHARS}]+", text.lower())


def _segments(text: str, allowed: frozenset[str]) -> bool:
    """Whether ``text`` (no separators) is made only of allowed words and digits."""

    if not text:
        return True
    if text[0].isdigit():
        index = 0
        while index < len(text) and text[index].isdigit():
            index += 1
        return _segments(text[index:], allowed)
    return any(text.startswith(word) and _segments(text[len(word):], allowed) for word in sorted(allowed, key=len, reverse=True) if word)


def names_entity(entity: Mapping[str, Any], *, handle: str, title: str) -> bool:
    """Whether an account's own handle or display name is the entity's, not a person's.

    The name (or an alias) must be all of it, or sit beside institutional
    words only: "bgscet_cse", "BGSCET Official" and "bgscetalumni" pass;
    "rahul_bgscet", "Rahul Kumar | BGSCET" and "in/rahul-kumar-bgscet-1234"
    do not, and neither does a bio that merely mentions the college. Short
    acronyms ("AIT") must stand as a whole word.
    """

    display = re.split(r"\s+\(@|\s+[•|·-]\s+|\s+on\s+(?:instagram|facebook|x|linkedin|youtube)\b", title, maxsplit=1, flags=re.IGNORECASE)[0]
    allowed = INSTITUTIONAL_WORDS | frozenset(word for location in entity.get("locations") or [] for word in _words(str(location)))
    # Glued handles ("bgscetalumni") are split only on words of three letters
    # or more, so short tokens ("it", "ai", "u") cannot spell a person's name.
    glued = frozenset(word for word in allowed if len(word) >= 3)
    for name in [entity["name"], *(entity.get("names") or [])]:
        name_words = _words(str(name))
        token = "".join(name_words)
        if not token:
            continue
        for candidate in (handle, display):
            words = _words(candidate)
            if not words:
                continue
            if words == name_words or "".join(words) == token:
                return True
            span = len(name_words)
            for start in range(len(words) - span + 1):
                if words[start : start + span] == name_words and all(word in allowed or word.isdigit() for word in words[:start] + words[start + span :]):
                    return True
            if len(token) >= 4:
                for word in words:
                    if token in word and word != token:
                        before, after = word.split(token, 1)
                        rest = [other for other in words if other is not word]
                        if _segments(before, glued) and _segments(after, glued) and all(other in allowed or other.isdigit() for other in rest):
                            return True
    return False


# Account keys that always belong to a person (or a phone number), whatever links them.
PERSONAL_PREFIXES = ("linkedin:in:", "reddit:u:", "whatsapp:")


def person_shaped(key: str) -> bool:
    return key.startswith(PERSONAL_PREFIXES) and not key.startswith("whatsapp:group:")


# -------------------------------------------------------------------- feeds
@dataclass(frozen=True, slots=True)
class FeedItem:
    link: str
    title: str
    published_at: datetime | None
    summary: str = ""  # the item's description / summary as plain text (news items are matched on it)


_DECLARATIONS = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# Descriptions often carry escaped HTML; only its text is kept.
_MARKUP = re.compile(r"<[^>]*>")


def parse_feed(body: bytes, *, limit: int = 100) -> tuple[str, list[FeedItem]] | None:
    """(feed title, items) from RSS 2.0, RSS 1.0 or Atom; None for anything else.

    Documents that declare a DOCTYPE or entities are refused outright, so no
    entity expansion or external lookup can happen whatever the XML parser.
    """

    from html import unescape

    if _DECLARATIONS.search(body):
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def child_text(element: ElementTree.Element, *names: str) -> str:
        for child in element:
            if local(child.tag) in names and (child.text or "").strip():
                return (child.text or "").strip()
        return ""

    items: list[FeedItem] = []
    title = ""
    kind = local(root.tag)
    if kind not in {"rss", "rdf", "feed"}:
        return None
    for element in root.iter():
        name = local(element.tag)
        if name in {"channel", "feed"} and not title:
            title = child_text(element, "title")
        if name not in {"item", "entry"}:
            continue
        link = child_text(element, "link")
        if not link:
            for child in element:
                if local(child.tag) == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
                    link = str(child.get("href"))
                    break
        published = child_text(element, "pubdate", "published", "updated", "date")
        summary = unescape(_MARKUP.sub(" ", child_text(element, "description", "summary")))
        items.append(FeedItem(link=link[:1000], title=child_text(element, "title")[:300], published_at=parse_published(published), summary=" ".join(summary.split())[:1000]))
        if len(items) >= limit:
            break
    if kind == "feed" and not title:
        title = child_text(root, "title")
    return title[:300], items


def unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = ["ApiClient", "ApiResponse", "EntityHit", "FAILED_OUTCOMES", "FeedItem", "INSTITUTIONAL_WORDS", "entity_profile", "match_entity", "names_entity", "parse_feed", "person_shaped", "unique"]
