"""Read a page's structure, not just its text: where each link sits and what the page declares.

The strongest evidence that an account is official is a link to it in the
header, navigation or footer of the institution's own site, or a JSON-LD
``sameAs`` / ``rel="me"`` declaration. The same parse also finds links that
are hidden with CSS (off-screen, zero-size, ``display:none``), which is how
spam injected into a hacked site hides; those must never vouch for anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

HEADER, NAV, FOOTER, BODY, ASIDE, HIDDEN = "header", "nav", "footer", "body", "aside", "hidden"
IDENTITY_POSITIONS = frozenset({HEADER, NAV, FOOTER})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|(?:left|top|right|text-indent)\s*:\s*-\s*\d{3,}(?:\.\d+)?\s*(?:px|em|rem|%)?|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])|"
    r"(?:width|height)\s*:\s*[01]px[^;]*;[^\"']*overflow\s*:\s*hidden|overflow\s*:\s*hidden[^\"']*(?:width|height)\s*:\s*[01]px",
    re.IGNORECASE,
)
_CLASS_POSITION = ((FOOTER, re.compile(r"(?:^|[\s_-])(?:footer|site-footer|foot)(?:$|[\s_-])", re.I)), (HEADER, re.compile(r"(?:^|[\s_-])(?:header|masthead|topbar|top-bar)(?:$|[\s_-])", re.I)), (NAV, re.compile(r"(?:^|[\s_-])(?:nav|navbar|menu|navigation)(?:$|[\s_-])", re.I)))


@dataclass(frozen=True, slots=True)
class PageLink:
    href: str  # absolute
    text: str
    position: str
    rel: tuple[str, ...] = ()


@dataclass(slots=True)
class PageStructure:
    url: str
    title: str = ""
    canonical: str | None = None
    links: list[PageLink] = field(default_factory=list)
    same_as: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def identity_links(self) -> list[PageLink]:
        """Visible links in the header, navigation or footer, plus rel="me" declarations."""

        return [link for link in self.links if link.position in IDENTITY_POSITIONS or ("me" in link.rel and link.position != HIDDEN)]

    def hidden_links(self) -> list[PageLink]:
        return [link for link in self.links if link.position == HIDDEN]

    def offsite_links(self, *, hidden: bool | None = None) -> list[PageLink]:
        host = _bare_host(self.url)
        return [link for link in self.links if _bare_host(link.href) != host and (hidden is None or (link.position == HIDDEN) == hidden)]


def _bare_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


class _StructureParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.stack: list[tuple[str, str | None, bool]] = []  # (tag, position set here, hidden set here)
        self.out = PageStructure(url=base_url)
        self._link: dict[str, object] | None = None
        self._in_title = False
        self._in_script: str | None = None
        self._script_buffer: list[str] = []
        self._text: list[str] = []

    # -- context ---------------------------------------------------------
    def _position(self) -> str:
        if any(hidden for _, _, hidden in self.stack):
            return HIDDEN
        for _, position, _ in reversed(self.stack):
            if position:
                return position
        return BODY

    @staticmethod
    def _element_position(tag: str, attributes: dict[str, str]) -> str | None:
        if tag in {"footer", "header", "nav", "aside"}:
            return {"footer": FOOTER, "header": HEADER, "nav": NAV, "aside": ASIDE}[tag]
        role = attributes.get("role", "").lower()
        if role == "contentinfo":
            return FOOTER
        if role == "banner":
            return HEADER
        if role == "navigation":
            return NAV
        marker = f"{attributes.get('id', '')} {attributes.get('class', '')}"
        for position, pattern in _CLASS_POSITION:
            if pattern.search(marker):
                return position
        return None

    @staticmethod
    def _element_hidden(attributes: dict[str, str]) -> bool:
        if "hidden" in attributes:
            return True
        style = attributes.get("style", "")
        return bool(style and _HIDDEN_STYLE.search(style))

    # -- events ----------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").lower()
            if key and "content" in attributes:
                self.out.meta.setdefault(key, attributes["content"][:500])
            return
        if tag == "link":
            rel = attributes.get("rel", "").lower().split()
            href = attributes.get("href", "")
            if href and "canonical" in rel and self.out.canonical is None:
                self.out.canonical = urljoin(self.base, href)
            if href and "alternate" in rel and attributes.get("type", "").lower() in {"application/rss+xml", "application/atom+xml"}:
                self.out.feeds.append(urljoin(self.base, href))
            if href and "me" in rel:
                self.out.links.append(PageLink(urljoin(self.base, href), "", HEAD_LINK, ("me",)))
            return
        if tag in _VOID:
            return
        if tag == "script":
            self._in_script = attributes.get("type", "").lower()
            self._script_buffer = []
        if tag == "title":
            self._in_title = True
        self.stack.append((tag, self._element_position(tag, attributes), self._element_hidden(attributes)))
        if tag == "a":
            href = attributes.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                self._link = {"href": urljoin(self.base, href), "text": [], "position": self._position(), "rel": tuple(attributes.get("rel", "").lower().split())}

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag == "a" and self._link is not None:
            self.out.links.append(PageLink(str(self._link["href"]), " ".join("".join(self._link["text"]).split())[:200], str(self._link["position"]), tuple(self._link["rel"])))  # type: ignore[arg-type]
            self._link = None
        if tag == "script" and self._in_script is not None:
            if "ld+json" in self._in_script:
                self._read_json_ld("".join(self._script_buffer))
            self._in_script = None
        if tag == "title":
            self._in_title = False
        # Close up to the matching tag; tolerate unclosed children in broken markup.
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_script is not None:
            self._script_buffer.append(data)
            return
        if self._in_title:
            self.out.title += data
            return
        if self._link is not None:
            self._link["text"].append(data)  # type: ignore[union-attr]
        if any(tag in {"style", "noscript", "template"} for tag, _, _ in self.stack):
            return
        if data.strip():
            self._text.append(data.strip())

    def _read_json_ld(self, raw: str) -> None:
        try:
            document = json.loads(raw)
        except ValueError:
            return
        found: list[str] = []

        def walk(node: object, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, dict):
                value = node.get("sameAs")
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(item for item in value if isinstance(item, str))
                for child in node.values():
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    walk(child, depth + 1)

        walk(document)
        for item in found[:50]:
            absolute = urljoin(self.base, item.strip())
            if absolute.startswith(("http://", "https://")) and absolute not in self.out.same_as:
                self.out.same_as.append(absolute)

    def result(self) -> PageStructure:
        if self._link is not None:
            self.handle_endtag("a")
        self.out.title = " ".join(self.out.title.split())[:300]
        self.out.text = " ".join(self._text)[:40_000]
        return self.out


HEAD_LINK = "head"


def parse_structure(html: str, url: str) -> PageStructure:
    parser = _StructureParser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup yields what was parsed so far
        pass
    return parser.result()


__all__ = ["ASIDE", "BODY", "FOOTER", "HEADER", "HEAD_LINK", "HIDDEN", "IDENTITY_POSITIONS", "NAV", "PageLink", "PageStructure", "parse_structure"]
