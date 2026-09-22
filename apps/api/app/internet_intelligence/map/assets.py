"""One stable key per public account, site or page, whatever URL form it was found under.

``instagram.com/BGSCET_Engg_Coll/``, ``www.instagram.com/bgscet_engg_coll?igsh=…``
and ``m.instagram.com/bgscet_engg_coll/reels`` are the same account; the map
must see one asset, not three. ``asset_ref`` reduces a URL to its platform,
its kind (account, domain, group, page) and a key that identifies it within
the platform. Keys are case-folded where the platform treats handles
case-insensitively and kept exact where it does not (YouTube channel IDs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from ..urls import canonicalize_url

ACCOUNT, DOMAIN, GROUP, PAGE = "account", "domain", "group", "page"

# host suffix -> platform
PLATFORM_HOSTS: tuple[tuple[str, str], ...] = (
    ("facebook.com", "facebook"), ("fb.com", "facebook"), ("fb.me", "facebook"), ("instagram.com", "instagram"), ("twitter.com", "x"), ("x.com", "x"),
    ("youtube.com", "youtube"), ("youtu.be", "youtube"), ("linkedin.com", "linkedin"), ("threads.net", "threads"), ("threads.com", "threads"),
    ("wa.me", "whatsapp"), ("wa.link", "whatsapp"), ("whatsapp.com", "whatsapp"), ("t.me", "telegram"), ("telegram.me", "telegram"), ("github.com", "github"),
    ("reddit.com", "reddit"), ("snapchat.com", "snapchat"), ("pinterest.com", "pinterest"), ("sharechat.com", "sharechat"), ("spotify.com", "spotify"),
    ("linktr.ee", "linktree"), ("bio.link", "linktree"), ("unstop.com", "unstop"), ("devpost.com", "devpost"), ("gdg.community.dev", "gdg"), ("quora.com", "quora"),
    ("wikipedia.org", "wikipedia"), ("wikidata.org", "wikidata"),
)
SOCIAL_PLATFORMS = frozenset({"facebook", "instagram", "x", "youtube", "linkedin", "threads", "whatsapp", "telegram", "github", "reddit", "snapchat", "pinterest", "sharechat", "spotify", "linktree", "quora"})
_RESERVED = {
    "instagram": {"p", "reel", "reels", "tv", "stories", "explore", "popular", "accounts", "direct"},
    "x": {"i", "home", "search", "intent", "share", "hashtag", "explore"},
    "facebook": {"sharer", "sharer.php", "dialog", "plugins", "watch", "events", "hashtag", "login", "photo.php", "story.php", "permalink.php", "share"},
    "threads": {"search", "intent"},
    "github": {"orgs", "sponsors", "topics", "search", "login"},
}
_FB_ID_TAIL = re.compile(r"(?:^|-)(\d{6,})$")
# "mailto:", "javascript:", "tel:" ... but not "host:8080/path".
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")


@dataclass(frozen=True, slots=True)
class AssetRef:
    platform: str  # "website" for ordinary sites
    kind: str  # account | domain | group | page
    key: str  # unique within an institution's map
    url: str  # canonical URL to show and to fetch
    handle: str  # short human label (@handle, domain, r/sub ...)

    @property
    def is_social(self) -> bool:
        return self.platform in SOCIAL_PLATFORMS


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for prefix in ("www.", "m.", "mobile.", "web.", "in.", "en.", "business."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    return host


def platform_of(url: str) -> str:
    host = _host(url)
    for suffix, platform in PLATFORM_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return platform
    return "website"


def asset_ref(url: str) -> AssetRef:
    """Reduce a URL to the asset it names; raises ValueError for non-web URLs."""

    text = url.strip()
    if "://" not in text:
        if _SCHEME.match(text):
            raise ValueError(f"not a web URL: {url!r}")
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"not a web URL: {url!r}")
    platform = platform_of(text)
    host = _host(text)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    first = segments[0] if segments else ""
    lowered = first.lower()

    def account(key: str, handle: str, path: str) -> AssetRef:
        return AssetRef(platform, ACCOUNT, f"{platform}:{key}", f"https://{_canonical_host(platform)}/{path}", handle)

    def page() -> AssetRef:
        canonical = canonicalize_url(text)
        return AssetRef(platform, PAGE, f"page:{canonical}", canonical, canonical.split("://", 1)[1][:80])

    if platform == "website":
        if not segments and not parsed.query:
            return AssetRef("website", DOMAIN, f"web:{host}", f"https://{host}/", host)
        return page()
    if platform in {"instagram", "threads"}:
        handle = lowered.removeprefix("@")
        if not handle or handle in _RESERVED.get(platform, set()):
            return page()
        return account(handle, f"@{handle}", f"@{handle}" if platform == "threads" else f"{handle}/")
    if platform == "x":
        if not segments or lowered in _RESERVED["x"] or (len(segments) >= 2 and segments[1].lower() == "status"):
            return page()
        return account(lowered, f"@{lowered}", lowered)
    if platform == "facebook":
        if lowered == "profile.php" and query.get("id"):
            number = query["id"][0]
            return account(f"id:{number}", f"profile {number}", f"profile.php?id={number}")
        if lowered == "groups" and len(segments) >= 2:
            return AssetRef(platform, GROUP, f"facebook:group:{segments[1].lower()}", f"https://www.facebook.com/groups/{segments[1]}/", f"group {segments[1]}")
        if not segments or lowered in _RESERVED["facebook"]:
            return page()
        # Numeric page IDs appear bare, at the end of /people/, /p/ and
        # /pages/ URLs, and on old "Page-Name-123456789" vanity URLs; all
        # of them name the page by its ID.
        candidates = segments if lowered in {"people", "pages", "p"} else segments[:1]
        for segment in reversed(candidates):
            found = _FB_ID_TAIL.search(segment)
            if found:
                return account(f"id:{found.group(1)}", f"page {found.group(1)}", found.group(1))
        if lowered in {"people", "pages", "p"}:
            return page()
        return account(lowered, lowered, lowered)
    if platform == "youtube":
        if lowered.startswith("@"):
            return account(lowered, lowered, lowered)
        if lowered == "channel" and len(segments) >= 2:
            return account(f"channel:{segments[1]}", f"channel/{segments[1][:12]}…", f"channel/{segments[1]}")
        if lowered in {"c", "user"} and len(segments) >= 2:
            return account(f"{lowered}:{segments[1].lower()}", segments[1], f"{lowered}/{segments[1]}")
        if lowered == "watch" and query.get("v"):
            video = query["v"][0]
            return AssetRef(platform, PAGE, f"youtube:video:{video}", f"https://www.youtube.com/watch?v={video}", f"video {video}")
        if host == "youtu.be" and segments:
            return AssetRef(platform, PAGE, f"youtube:video:{segments[0]}", f"https://www.youtube.com/watch?v={segments[0]}", f"video {segments[0]}")
        return page()
    if platform == "linkedin":
        if lowered in {"company", "school", "showcase", "in"} and len(segments) >= 2:
            slug = segments[1].lower()
            return account(f"{lowered}:{slug}", f"{lowered}/{slug}", f"{lowered}/{slug}/")
        return page()
    if platform == "whatsapp":
        if host == "chat.whatsapp.com" and segments:
            return AssetRef(platform, GROUP, f"whatsapp:group:{segments[0]}", f"https://chat.whatsapp.com/{segments[0]}", "WhatsApp group")
        if host == "wa.me" and segments and segments[0].isdigit():
            return account(segments[0], f"+{segments[0]}", segments[0])
        return page()
    if platform == "telegram":
        if lowered in {"joinchat", "+"} or first.startswith("+"):
            return AssetRef(platform, GROUP, f"telegram:group:{segments[-1]}", canonicalize_url(text), "Telegram invite")
        return account(lowered, f"t.me/{lowered}", lowered) if segments else page()
    if platform == "github":
        if not segments or lowered in _RESERVED["github"]:
            return page()
        return account(lowered, lowered, lowered)
    if platform == "reddit":
        if lowered == "r" and len(segments) >= 2:
            if len(segments) >= 4 and segments[2].lower() == "comments":
                return AssetRef(platform, PAGE, f"reddit:post:{segments[3].lower()}", f"https://www.reddit.com/r/{segments[1]}/comments/{segments[3]}/", f"r/{segments[1]} post")
            sub = segments[1].lower()
            return AssetRef(platform, GROUP, f"reddit:r:{sub}", f"https://www.reddit.com/r/{sub}/", f"r/{sub}")
        if lowered in {"user", "u"} and len(segments) >= 2:
            return account(f"u:{segments[1].lower()}", f"u/{segments[1]}", f"user/{segments[1]}")
        return page()
    if platform == "linktree":
        return account(lowered, lowered, lowered) if segments else page()
    if platform in {"sharechat", "pinterest", "snapchat"} and segments:
        if platform == "sharechat" and lowered == "profile" and len(segments) >= 2:
            return account(segments[1].lower(), segments[1], f"profile/{segments[1]}")
        if platform == "snapchat" and lowered == "add" and len(segments) >= 2:
            return account(segments[1].lower(), segments[1], f"add/{segments[1]}")
        if platform == "pinterest" and len(segments) >= 2:
            return AssetRef(platform, PAGE, f"pinterest:{lowered}/{segments[1].lower()}", canonicalize_url(text), f"{first}/{segments[1]}")
    return page()


def _canonical_host(platform: str) -> str:
    return {
        "facebook": "www.facebook.com", "instagram": "www.instagram.com", "x": "x.com", "youtube": "www.youtube.com", "linkedin": "www.linkedin.com", "threads": "www.threads.com",
        "whatsapp": "wa.me", "telegram": "t.me", "github": "github.com", "reddit": "www.reddit.com", "linktree": "linktr.ee", "sharechat": "sharechat.com", "snapchat": "www.snapchat.com",
    }.get(platform, platform)


__all__ = ["ACCOUNT", "DOMAIN", "GROUP", "PAGE", "SOCIAL_PLATFORMS", "AssetRef", "asset_ref", "platform_of"]
