"""Transport-neutral voice event names."""

from enum import StrEnum


class RealtimeEvent(StrEnum):
    SESSION_CREATED = "session.created"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    ERROR = "error"
    SESSION_CLOSED = "session.closed"


__all__ = ["RealtimeEvent"]
