"""Bounded voice-session manager with ephemeral transport credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from secrets import token_urlsafe

from ..domain.principals import Capability, InstitutionScope, Principal


DEFAULT_COLLEGE_ID = "college_a"


class VoiceScopeError(PermissionError):
    """Raised when a voice session requests a scope outside the principal grant."""



def _default_scope(principal: Principal) -> InstitutionScope:
    if principal.scopes:
        return principal.scopes[0]
    return InstitutionScope(DEFAULT_COLLEGE_ID)


@dataclass(frozen=True, slots=True)
class VoiceSession:
    session_id: str
    principal_id: str
    created_at: datetime
    expires_at: datetime
    principal: Principal = field(repr=False, compare=False)
    institution_scope: InstitutionScope = field(repr=False, compare=False)
    ticket_digest: str = field(repr=False, compare=False)
    status: str = "active"
    audio_retention: str = "ephemeral"


class VoiceSessionManager:
    def __init__(self, ttl_seconds: int = 300, max_active: int = 10) -> None:
        if ttl_seconds <= 0 or max_active <= 0:
            raise ValueError("voice limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_active = max_active
        self._sessions: dict[str, VoiceSession] = {}
        self._active_transports: set[str] = set()
        self._consumed_tickets: set[str] = set()

    def _create(
        self,
        principal: Principal,
        institution_scope: InstitutionScope | None = None,
    ) -> tuple[VoiceSession, str]:
        if not principal.authenticated or Capability.START_VOICE_SESSION not in principal.capabilities:
            raise PermissionError("voice:start capability is required")
        scope = institution_scope or _default_scope(principal)
        if not principal.can_access(scope):
            raise VoiceScopeError("voice session scope is outside the authenticated institution scope")
        self._expire()
        if len(self._sessions) >= self.max_active:
            raise RuntimeError("live voice session limit reached")

        now = datetime.now(timezone.utc)
        session_id = token_urlsafe(18)
        transport_ticket = token_urlsafe(32)
        session = VoiceSession(
            session_id=session_id,
            principal_id=principal.principal_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            principal=principal,
            institution_scope=scope,
            ticket_digest=self._digest(transport_ticket),
        )
        self._sessions[session.session_id] = session
        return session, transport_ticket

    def create(
        self,
        principal: Principal,
        institution_scope: InstitutionScope | None = None,
    ) -> VoiceSession:
        """Create a session while preserving the original unit-test/API shape."""

        session, _ = self._create(principal, institution_scope)
        return session

    def create_access(
        self,
        principal: Principal,
        institution_scope: InstitutionScope | None = None,
    ) -> tuple[VoiceSession, str]:
        """Create a session and return its one-use WebSocket ticket once."""

        return self._create(principal, institution_scope)

    def get(self, session_id: str) -> VoiceSession | None:
        self._expire()
        return self._sessions.get(session_id)

    def claim_transport(self, session_id: str, transport_ticket: str) -> Principal | None:
        """Consume a ticket and bind one WebSocket to the verified principal."""

        session = self.get(session_id)
        if session is None or session_id in self._active_transports or session_id in self._consumed_tickets:
            return None
        if not transport_ticket or not hmac.compare_digest(
            session.ticket_digest,
            self._digest(transport_ticket),
        ):
            return None
        self._consumed_tickets.add(session_id)
        self._active_transports.add(session_id)
        return session.principal

    def release_transport(self, session_id: str) -> None:
        self._active_transports.discard(session_id)

    def close(self, session_id: str, principal_id: str) -> bool:
        session = self.get(session_id)
        if session is None or session.principal_id != principal_id:
            return False
        del self._sessions[session_id]
        self._active_transports.discard(session_id)
        self._consumed_tickets.discard(session_id)
        return True

    def active(self) -> tuple[VoiceSession, ...]:
        self._expire()
        return tuple(self._sessions.values())

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _expire(self) -> None:
        now = datetime.now(timezone.utc)
        for key, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[key]
                self._active_transports.discard(key)
                self._consumed_tickets.discard(key)


__all__ = ["VoiceScopeError", "VoiceSession", "VoiceSessionManager"]
