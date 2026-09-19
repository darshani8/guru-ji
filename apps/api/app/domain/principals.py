"""Identity and institution-scope primitives for Guru Ji."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PrincipalType(StrEnum):
    """Supported identity categories; permissions are granted separately."""

    ANONYMOUS = "anonymous"
    STUDENT = "student"
    FACULTY = "faculty"
    MAIN_ADMIN = "main_admin"
    SYSTEM = "system"


class Capability(StrEnum):
    """Small, auditable capabilities used by policy checks."""

    ASK_READ_ONLY = "ask:read_only"
    START_VOICE_SESSION = "voice:start"
    VIEW_SOURCE_METADATA = "source:view_metadata"
    RUN_BRIEFING = "briefing:run"
    VIEW_BRIEFING_HISTORY = "briefing:view_history"
    MANAGE_ACCESS = "access:manage"


@dataclass(frozen=True, slots=True)
class InstitutionScope:
    """The narrowest institutional scope a request or grant can name."""

    college_id: str
    department_id: str | None = None
    batch_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("college_id", "department_id", "batch_id"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must not be blank")

    def covers(self, requested: "InstitutionScope") -> bool:
        """Return whether this granted scope contains the requested scope."""

        if self.college_id != requested.college_id:
            return False

        for granted_value, requested_value in (
            (self.department_id, requested.department_id),
            (self.batch_id, requested.batch_id),
        ):
            if requested_value is None:
                if granted_value is not None:
                    return False
            elif granted_value is not None and granted_value != requested_value:
                return False

        return True

    def as_dict(self) -> dict[str, str | None]:
        return {
            "college_id": self.college_id,
            "department_id": self.department_id,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller with explicit capabilities and scopes.

    ``consent_verified`` and ``revoked`` are server-derived identity state. They
    are intentionally not inferred from client-supplied role or capability
    headers. A revoked principal remains representable for audit/PDP tests, but
    is never active for route authorization.
    """

    principal_id: str
    principal_type: PrincipalType
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    scopes: tuple[InstitutionScope, ...] = ()
    authenticated: bool = True
    consent_verified: bool = False
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be blank")
        if self.principal_type is PrincipalType.ANONYMOUS and (
            self.authenticated or self.capabilities or self.scopes or self.consent_verified or self.revoked
        ):
            raise ValueError("anonymous principals cannot carry authority")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "scopes", tuple(self.scopes))

    @property
    def active(self) -> bool:
        return self.authenticated and not self.revoked and self.principal_type is not PrincipalType.ANONYMOUS

    def has_capability(self, capability: Capability) -> bool:
        """Check one explicit capability without granting anything implicitly."""

        return self.active and capability in self.capabilities

    def can_access(self, requested_scope: InstitutionScope) -> bool:
        """Check whether at least one granted scope contains the request scope."""

        return self.active and any(scope.covers(requested_scope) for scope in self.scopes)


__all__ = [
    "Capability",
    "InstitutionScope",
    "Principal",
    "PrincipalType",
]
