"""Identity and institution-scope primitives for Agentic Saffron."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PrincipalType(StrEnum):
    """Supported identity categories; permissions are granted separately."""

    ANONYMOUS = "anonymous"
    STUDENT = "student"
    FACULTY = "faculty"
    STAFF = "staff"
    HOD = "hod"
    PRINCIPAL = "principal"
    INSTITUTION_ADMIN = "institution_admin"
    MAIN_ADMIN = "main_admin"
    PLATFORM_SUPER_ADMIN = "platform_super_admin"
    SYSTEM = "system"


class Capability(StrEnum):
    """Small, auditable capabilities used by policy checks."""

    ASK_READ_ONLY = "ask:read_only"
    START_VOICE_SESSION = "voice:start"
    VIEW_SOURCE_METADATA = "source:view_metadata"
    RUN_BRIEFING = "briefing:run"
    VIEW_BRIEFING_HISTORY = "briefing:view_history"
    MANAGE_ACCESS = "access:manage"
    # Institutional data platform capabilities. Each maps to one tool group in
    # the tool gateway; none of them is implied by another.
    AGENT_COMMAND = "agent:command"
    DATA_INGEST = "data:ingest"
    DATA_REVIEW = "data:review"
    STUDENTS_READ = "students:read"
    STUDENTS_READ_CONTACT = "students:read_contact"
    ATTENDANCE_READ = "attendance:read"
    FEES_READ = "fees:read"
    FACULTY_READ = "faculty:read"
    EXAMS_READ = "exams:read"
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_MANAGE = "documents:manage"
    REPORTS_GENERATE = "reports:generate"
    ACTIONS_EMAIL = "actions:email"
    ACTIONS_NOTIFY = "actions:notify"
    RECORDS_WRITE = "records:write"
    INTELLIGENCE_READ = "intelligence:read"
    INTELLIGENCE_MANAGE = "intelligence:manage"
    # Open-web search from the assistant ("search the internet for ..."). Kept
    # apart from ask:read_only because the query leaves the institution.
    WEB_SEARCH = "web:search"


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


def principal_snapshot(principal: Principal) -> dict[str, Any]:
    """The verified principal as plain data, for work that outlives the request (jobs, voice sessions)."""

    return {
        "principal_id": principal.principal_id,
        "principal_type": principal.principal_type.value,
        "capabilities": sorted(item.value for item in principal.capabilities),
        "scopes": [scope.as_dict() for scope in principal.scopes],
        "consent_verified": principal.consent_verified,
    }


def principal_from_snapshot(snapshot: Mapping[str, Any]) -> Principal:
    """Rebuild a principal the server itself recorded; unknown capabilities are dropped, never granted."""

    capabilities = frozenset(Capability(item) for item in snapshot.get("capabilities", []) if item in Capability._value2member_map_)
    scopes = tuple(InstitutionScope(str(item["college_id"]), item.get("department_id"), item.get("batch_id")) for item in snapshot.get("scopes", []) if isinstance(item, Mapping) and item.get("college_id"))
    return Principal(str(snapshot["principal_id"]), PrincipalType(str(snapshot.get("principal_type", "student"))), capabilities, scopes, authenticated=True, consent_verified=bool(snapshot.get("consent_verified", False)))


__all__ = [
    "Capability",
    "InstitutionScope",
    "Principal",
    "PrincipalType",
    "principal_from_snapshot",
    "principal_snapshot",
]
