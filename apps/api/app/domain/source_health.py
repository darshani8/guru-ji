"""Source health and freshness contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class SourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    institution_id: str
    status: SourceHealthStatus
    checked_at: datetime
    display_name: str = ""
    connector_type: str = ""
    last_success_at: datetime | None = None
    latency_ms: int | None = None
    freshness: Freshness = Freshness.UNKNOWN
    detail: str = ""

    def __post_init__(self) -> None:
        for field_name in ("source_id", "institution_id"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        status = self.status
        if isinstance(status, str):
            status = SourceHealthStatus(status)
        object.__setattr__(self, "status", status)
        freshness = self.freshness
        if isinstance(freshness, str):
            freshness = Freshness(freshness)
        object.__setattr__(self, "freshness", freshness)
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")


__all__ = ["Freshness", "SourceHealth", "SourceHealthStatus"]
