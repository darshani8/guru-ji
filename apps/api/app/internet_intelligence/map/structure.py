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
# Whole class or id tokens that name the page's own chrome. Substrings do not
# count: "card-footer", "modal-header" and a theme's body class such as
# "et_pb_footer_columns4" describe content, not the site's footer.
_CHROME_TOKENS: dict[str, frozenset[str]] = {
    FOOTER: frozenset({"footer", "site-footer", "main-footer", "global-footer", "footer-widgets", "colophon"}),
    HEADER: frozenset({"header", "site-header", "main-header", "global-header", "masthead", "topbar", "top-bar"}),
    NAV: frozenset({"nav", "navbar", "navigation", "site-navigation", "main-navigation", "primary-menu", "main-menu"}),
}
# A <header> or <footer> inside one of these belongs to that piece of content
# (an article's byline, a card), not to the page.
_SECTIONING = frozenset({"article", "section", "aside", "main", "blockquote"})
# Elements whose classes say nothing about where a link sits.
_NO_CLASS_POSITION = frozenset({"html", "body", "a"})
# Organisation types whose JSON-LD sameAs declares the site owner's accounts.
_ORGANISATION_TYPES = frozenset({
    "organization", "educationalorganization", "collegeoruniversity", "school", "highschool", "middleschool", "elementaryschool", "preschool",
    "ngo", "governmentorganization", "localbusiness", "corporation", "researchorganization", "medicalorganization", "hospital", "placeofworship", "hindutemple",
})


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

    def _element_position(self, tag: str, attributes: dict[str, str]) -> str | None:
        """The page region an element opens, if it is the site's own header, navigation or footer."""

        if tag == "aside":
            return ASIDE
        inside_content = any(open_tag in _SECTIONING for open_tag, _, _ in self.stack)
        landmark = {"footer": FOOTER, "header": HEADER, "nav": NAV}.get(tag) or {"contentinfo": FOOTER, "banner": HEADER, "navigation": NAV}.get(attributes.get("role", "").lower())
        if landmark:
            return None if inside_content else landmark
        if tag in _NO_CLASS_POSITION:
            return None
        tokens = {token.lower() for token in attributes.get("class", "").split()} | ({attributes["id"].strip().lower()} if attributes.get("id", "").strip() else set())
        for position, names in _CHROME_TOKENS.items():
            if tokens & names:
                return None if inside_content else position
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
        page_host = _bare_host(self.base)

        def is_organisation(node: dict) -> bool:
            kinds = node.get("@type")
            kinds = [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
            return any(isinstance(kind, str) and kind.lower() in _ORGANISATION_TYPES for kind in kinds)

        def about_this_site(node: dict) -> bool:
            # An organisation node that names a different site describes someone else.
            for key in ("url", "@id"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")) and _bare_host(value) not in {page_host, ""}:
                    return False
            return True

        def take(node: object) -> None:
            if isinstance(node, dict) and is_organisation(node) and about_this_site(node):
                value = node.get("sameAs")
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(item for item in value if isinstance(item, str))

        # Only the site owner's organisation node counts: a top-level node, an
        # @graph entry, or the publisher / provider of the site or page. Never
        # a founder, author, employee or other nested person or organisation.
        roots = document if isinstance(document, list) else [document]
        candidates: list[object] = []
        for root in roots:
            if not isinstance(root, dict):
                continue
            candidates.append(root)
            graph = root.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)
        for node in list(candidates):
            if isinstance(node, dict):
                for key in ("publisher", "provider"):
                    if isinstance(node.get(key), dict):
                        candidates.append(node[key])
        for node in candidates:
            take(node)
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
