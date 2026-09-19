"""Allowlisted, bounded extraction for public-web search results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .citations import WebCitation
from .domain_allowlist import DEFAULT_ALLOWED_DOMAINS, is_allowed
from .http_transport import WebPayloadTooLarge, read_bounded
from .untrusted_content import UntrustedContent, safe_excerpt, wrap_untrusted


class WebExtractionError(RuntimeError):
    """Base class for safe page-extraction failures."""


class WebExtractionUnavailable(WebExtractionError):
    """Raised when an allowlisted page cannot be fetched or parsed safely."""


class _VisibleTextParser(HTMLParser):
    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif normalized == "title" and self._ignored_depth == 0:
            self._in_title = True
        elif normalized in {"br", "p", "div", "li", "section", "article", "h1", "h2", "h3"} and self._ignored_depth == 0:
            self.text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif normalized == "title":
            self._in_title = False
        elif normalized in {"br", "p", "div", "li", "section", "article", "h1", "h2", "h3"} and self._ignored_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    @staticmethod
    def _normalize(parts: Iterable[str]) -> str:
        lines = [" ".join(part.split()) for part in parts]
        return "\n".join(line for line in lines if line).strip()

    @property
    def title(self) -> str:
        return self._normalize(self.title_parts)

    @property
    def text(self) -> str:
        return self._normalize(self.text_parts)


@dataclass(frozen=True, slots=True)
class ExtractedWebPage:
    url: str
    title: str
    content: UntrustedContent
    citation: WebCitation
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.content.warnings


@dataclass(frozen=True, slots=True)
class AllowlistedHttpExtractor:
    allowed_domains: frozenset[str] = DEFAULT_ALLOWED_DOMAINS
    timeout_seconds: float = 8.0
    max_response_bytes: int = 1_000_000
    max_excerpt_chars: int = 2_000
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("web extraction timeout must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("web extraction max response bytes must be positive")
        if self.max_excerpt_chars <= 0:
            raise ValueError("web extraction max excerpt chars must be positive")
        normalized = frozenset(item.strip().lower().rstrip(".") for item in self.allowed_domains if item.strip())
        if not normalized:
            raise ValueError("web extraction requires at least one allowed domain")
        object.__setattr__(self, "allowed_domains", normalized)

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WebExtractionUnavailable("public-web URL was not an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise WebExtractionUnavailable("public-web URL contained credentials")
        if not is_allowed(url, self.allowed_domains):
            raise WebExtractionUnavailable("public-web URL is outside the configured allowlist")

    async def extract(self, url: str) -> ExtractedWebPage:
        self._validate_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", url, headers={"Accept": "text/html,text/plain;q=0.8"}) as response:
                    if 300 <= response.status_code < 400:
                        raise WebExtractionUnavailable("public-web redirects are not followed")
                    response.raise_for_status()
                    raw = await read_bounded(response, self.max_response_bytes)
                    content_type = response.headers.get("content-type", "").lower()
                    encoding = response.encoding or "utf-8"
        except WebExtractionUnavailable:
            raise
        except httpx.TimeoutException as exc:
            raise WebExtractionUnavailable("allowlisted public-web page timed out") from exc
        except (httpx.HTTPError, WebPayloadTooLarge, ValueError) as exc:
            raise WebExtractionUnavailable("allowlisted public-web page was unavailable or invalid") from exc

        if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
            raise WebExtractionUnavailable("allowlisted public-web page was not readable text")
        try:
            text = raw.decode(encoding, errors="replace")
            parser = _VisibleTextParser()
            parser.feed(text)
            parser.close()
        except (UnicodeError, ValueError) as exc:
            raise WebExtractionUnavailable("allowlisted public-web page could not be parsed") from exc

        visible_text = parser.text[: self.max_excerpt_chars]
        if not visible_text:
            raise WebExtractionUnavailable("allowlisted public-web page contained no readable text")
        title = parser.title[:300] or (urlparse(url).hostname or url)
        content = wrap_untrusted(url, visible_text)
        retrieved_at = datetime.now(timezone.utc)
        citation = WebCitation(
            url=url,
            title=title,
            excerpt=safe_excerpt(content, self.max_excerpt_chars),
            retrieved_at=retrieved_at,
        )
        return ExtractedWebPage(
            url=url,
            title=title,
            content=content,
            citation=citation,
            retrieved_at=retrieved_at,
        )


__all__ = [
    "AllowlistedHttpExtractor",
    "ExtractedWebPage",
    "WebExtractionError",
    "WebExtractionUnavailable",
]
