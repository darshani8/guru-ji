"""Provenance contracts for every source-derived result."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def _require_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _require_aware(field_name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class SourceKind(StrEnum):
    INTERNAL_DATABASE = "internal_database"
    INTERNAL_API = "internal_api"
    PUBLIC_WEB = "public_web"
    CONTROL_PLANE = "control_plane"


@dataclass(frozen=True, slots=True)
class DataPeriod:
    """The reporting interval represented by a source result."""

    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        started_at = _require_aware("started_at", self.started_at)
        ended_at = _require_aware("ended_at", self.ended_at)
        if ended_at < started_at:
            raise ValueError("ended_at must not precede started_at")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "ended_at", ended_at)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Evidence metadata that travels with a result but not sensitive records."""

    source_id: str
    source_type: SourceKind
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    complete: bool = True
    rows_used: int = 0
    data_period: DataPeriod | None = None
    redactions_applied: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _require_text("source_id", self.source_id))
        source_type = self.source_type
        if isinstance(source_type, str):
            source_type = SourceKind(source_type)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "retrieved_at", _require_aware("retrieved_at", self.retrieved_at))
        if self.rows_used < 0:
            raise ValueError("rows_used must not be negative")
        object.__setattr__(self, "redactions_applied", tuple(_require_text("redaction", item) for item in self.redactions_applied))


@dataclass(frozen=True, slots=True)
class Warning:
    """A user-visible limitation or partial-result warning."""

    code: str
    message: str
    source_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_text("code", self.code))
        object.__setattr__(self, "message", _require_text("message", self.message))
        if self.source_id is not None:
            object.__setattr__(self, "source_id", _require_text("source_id", self.source_id))


__all__ = ["DataPeriod", "Provenance", "SourceKind", "Warning"]
