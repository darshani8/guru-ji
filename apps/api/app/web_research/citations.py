"""Citation records for optional public-web context."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebCitation:
    url: str
    title: str
    excerpt: str


__all__ = ["WebCitation"]
