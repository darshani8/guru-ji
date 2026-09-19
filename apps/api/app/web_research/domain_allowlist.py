"""Explicit domain policy for optional web context."""

from urllib.parse import urlparse

DEFAULT_ALLOWED_DOMAINS = frozenset({"india.gov.in", "education.gov.in", "ugc.gov.in", "aicte-india.org"})


def is_allowed(url: str, allowed_domains: frozenset[str] = DEFAULT_ALLOWED_DOMAINS) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)


__all__ = ["DEFAULT_ALLOWED_DOMAINS", "is_allowed"]
