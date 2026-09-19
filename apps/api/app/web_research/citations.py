"""Citation records for optional public-web context."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class WebCitation:
    url: str
    title: str
    excerpt: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.url.strip() or not self.title.strip():
            raise ValueError("web citation URL and title must not be blank")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("web citation retrieved_at must be timezone-aware")


__all__ = ["WebCitation"]
