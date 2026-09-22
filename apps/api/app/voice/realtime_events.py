"""Transport-neutral voice event names for the browser WebSocket."""

from enum import StrEnum


class RealtimeEvent(StrEnum):
    AUTHENTICATE = "auth"
    READY = "ready"
    SESSION_CREATED = "session.created"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    UTTERANCE = "utterance"
    ANSWER = "answer"
    PING = "ping"
    PONG = "pong"
    CLOSE = "close"
    SESSION_CLOSED = "session.closed"
    ERROR = "error"
    EXPIRED = "expired"


__all__ = ["RealtimeEvent"]
