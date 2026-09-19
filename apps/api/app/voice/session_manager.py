"""Bounded voice-session manager with no persistent audio storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

from ..domain.principals import Capability, Principal


@dataclass(frozen=True, slots=True)
class VoiceSession:
    session_id: str
    principal_id: str
    created_at: datetime
    expires_at: datetime
    status: str = "active"
    audio_retention: str = "ephemeral"


class VoiceSessionManager:
    def __init__(self, ttl_seconds: int = 300, max_active: int = 10) -> None:
        if ttl_seconds <= 0 or max_active <= 0:
            raise ValueError("voice limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_active = max_active
        self._sessions: dict[str, VoiceSession] = {}

    def create(self, principal: Principal) -> VoiceSession:
        if not principal.authenticated or Capability.START_VOICE_SESSION not in principal.capabilities:
            raise PermissionError("voice:start capability is required")
        self._expire()
        if len(self._sessions) >= self.max_active:
            raise RuntimeError("live voice session limit reached")
        now = datetime.now(timezone.utc)
        session = VoiceSession(token_urlsafe(18), principal.principal_id, now, now + timedelta(seconds=self.ttl_seconds))
        self._sessions[session.session_id] = session
        return session

    def close(self, session_id: str, principal_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.principal_id != principal_id:
            return False
        del self._sessions[session_id]
        return True

    def active(self) -> tuple[VoiceSession, ...]:
        self._expire()
        return tuple(self._sessions.values())

    def _expire(self) -> None:
        now = datetime.now(timezone.utc)
        for key, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[key]


__all__ = ["VoiceSession", "VoiceSessionManager"]
