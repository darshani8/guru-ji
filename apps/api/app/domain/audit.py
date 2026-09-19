"""Audit event contracts without raw sensitive payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: str
    request_id: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    principal_id: str | None = None
    endpoint: str | None = None
    conversation_id: str | None = None
    source_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    redactions_applied: tuple[str, ...] = ()
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "event_type", "request_id"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        outcome = self.outcome
        if isinstance(outcome, str):
            outcome = AuditOutcome(outcome)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        object.__setattr__(self, "redactions_applied", tuple(self.redactions_applied))
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")


__all__ = ["AuditEvent", "AuditOutcome"]
