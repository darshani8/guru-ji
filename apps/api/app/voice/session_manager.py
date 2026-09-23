"""Bounded voice-session manager with one-use transport tickets.

A session is a control-store row, so the request that opens it and the
WebSocket that uses it may reach different API tasks, and a close or a
replayed ticket is seen by every task sharing the database. Without a store
(unit tests, a single local process) the rows live in memory. The ticket is
kept only as a SHA-256 digest and is claimed by one conditional update, so it
opens exactly one socket, once, within its short lifetime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from typing import Any, Protocol

from ..domain.principals import Capability, InstitutionScope, Principal, principal_from_snapshot, principal_snapshot
from ..persistence.database import InMemoryControlStore, timestamp


DEFAULT_COLLEGE_ID = "college_a"


class VoiceScopeError(PermissionError):
    """Raised when a voice session requests a scope outside the principal grant."""


class VoiceSessionStore(Protocol):
    def create_voice_session(self, record: dict[str, Any]) -> None: ...
    def get_voice_session(self, session_id: str) -> dict[str, Any] | None: ...
    def claim_voice_session(self, session_id: str, ticket_sha256: str, now: str) -> dict[str, Any] | None: ...
    def close_voice_session(self, session_id: str, principal_id: str | None, now: str) -> bool: ...
    def active_voice_sessions(self, now: str, principal_id: str | None = None) -> list[dict[str, Any]]: ...


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
    ticket_expires_at: datetime | None = field(default=None, compare=False)
    status: str = "active"
    audio_retention: str = "ephemeral"


def _session_from_row(row: dict[str, Any]) -> VoiceSession:
    snapshot = json.loads(row["principal_json"])
    scope_data = snapshot.pop("session_scope", None) or {"college_id": row["institution_id"]}
    return VoiceSession(
        session_id=row["session_id"],
        principal_id=row["principal_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        principal=principal_from_snapshot(snapshot),
        institution_scope=InstitutionScope(str(scope_data["college_id"]), scope_data.get("department_id"), scope_data.get("batch_id")),
        ticket_expires_at=datetime.fromisoformat(row["ticket_expires_at"]),
        status="closed" if row.get("closed_at") else "active",
    )


class VoiceSessionManager:
    def __init__(
        self,
        ttl_seconds: int = 300,
        max_active: int = 10,
        *,
        store: VoiceSessionStore | None = None,
        ticket_ttl_seconds: int = 60,
        max_per_person: int = 2,
    ) -> None:
        if ttl_seconds <= 0 or max_active <= 0 or ticket_ttl_seconds <= 0 or max_per_person <= 0:
            raise ValueError("voice limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_active = max_active
        self.ticket_ttl_seconds = min(ticket_ttl_seconds, ttl_seconds)
        self.max_per_person = max_per_person
        self._store: VoiceSessionStore = store if store is not None else InMemoryControlStore()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

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
        now = self._now()
        stamp = timestamp(now)
        # Opening the assistant again (a reload, a second tab) replaces the
        # person's oldest conversation rather than locking them out of voice.
        own = self._store.active_voice_sessions(stamp, principal.principal_id)
        for stale in own[: max(0, len(own) - self.max_per_person + 1)]:
            self._store.close_voice_session(stale["session_id"], principal.principal_id, stamp)
        if len(self._store.active_voice_sessions(stamp)) >= self.max_active:
            raise RuntimeError("live voice session limit reached")

        session_id = token_urlsafe(18)
        transport_ticket = token_urlsafe(32)
        snapshot = {**principal_snapshot(principal), "session_scope": scope.as_dict()}
        ticket_expires_at = now + timedelta(seconds=self.ticket_ttl_seconds)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        self._store.create_voice_session({
            "session_id": session_id,
            "principal_id": principal.principal_id,
            "institution_id": scope.college_id,
            "principal_json": json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
            "ticket_sha256": self._digest(transport_ticket),
            "created_at": stamp,
            "ticket_expires_at": timestamp(ticket_expires_at),
            "expires_at": timestamp(expires_at),
            "claimed_at": None,
            "closed_at": None,
        })
        session = VoiceSession(
            session_id=session_id,
            principal_id=principal.principal_id,
            created_at=now,
            expires_at=expires_at,
            principal=principal,
            institution_scope=scope,
            ticket_expires_at=ticket_expires_at,
        )
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
        """The session while it is open and unexpired; None once closed or expired."""

        row = self._store.get_voice_session(session_id)
        if row is None or row.get("closed_at") or row["expires_at"] <= timestamp(self._now()):
            return None
        return _session_from_row(row)

    def claim_transport(self, session_id: str, transport_ticket: str) -> Principal | None:
        """Consume a ticket and bind one WebSocket to the verified principal."""

        if not transport_ticket:
            return None
        row = self._store.claim_voice_session(session_id, self._digest(transport_ticket), timestamp(self._now()))
        return _session_from_row(row).principal if row else None

    def release_transport(self, session_id: str) -> None:
        """The socket is gone: the session ends with it (a claimed ticket never reopens it)."""

        self._store.close_voice_session(session_id, None, timestamp(self._now()))

    def close(self, session_id: str, principal_id: str) -> bool:
        return self._store.close_voice_session(session_id, principal_id, timestamp(self._now()))

    def active(self) -> tuple[VoiceSession, ...]:
        return tuple(_session_from_row(row) for row in self._store.active_voice_sessions(timestamp(self._now())))


__all__ = ["VoiceScopeError", "VoiceSession", "VoiceSessionManager", "VoiceSessionStore"]
