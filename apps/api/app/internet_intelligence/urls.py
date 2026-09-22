"""URL canonicalisation for deduplication and source classification."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "_ga")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be absolute HTTP(S)")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=False) if not key.lower().startswith(_TRACKING_PREFIXES)]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(("https" if parsed.scheme == "https" else "http", host, path, "", urlencode(sorted(query)), ""))


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


__all__ = ["canonicalize_url", "domain_of"]
