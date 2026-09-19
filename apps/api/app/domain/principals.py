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


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller with explicit capabilities and scopes."""

    principal_id: str
    principal_type: PrincipalType
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    scopes: tuple[InstitutionScope, ...] = ()
    authenticated: bool = True

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be blank")
        if self.principal_type is PrincipalType.ANONYMOUS and (
            self.authenticated or self.capabilities or self.scopes
        ):
            raise ValueError("anonymous principals cannot carry authority")

    def has_capability(self, capability: Capability) -> bool:
        """Check one explicit capability without granting anything implicitly."""

        return capability in self.capabilities

    def can_access(self, requested_scope: InstitutionScope) -> bool:
        """Check whether at least one granted scope contains the request scope."""

        return any(scope.covers(requested_scope) for scope in self.scopes)


__all__ = [
    "Capability",
    "InstitutionScope",
    "Principal",
    "PrincipalType",
]
