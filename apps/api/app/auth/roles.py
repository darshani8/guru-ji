"""Single role-to-capability map shared by every identity adapter.

Roles come from a verified identity source (OIDC claims, a trusted LMS edge, or
the development demo headers). Capabilities are the only thing policy checks
consult, so the mapping lives in one place and stays deny-by-default: a role
that is not listed here receives no capability at all.
"""

from __future__ import annotations

from ..domain.principals import Capability, PrincipalType

_STUDENT = frozenset({
    Capability.ASK_READ_ONLY,
    Capability.START_VOICE_SESSION,
    Capability.AGENT_COMMAND,
    Capability.DOCUMENTS_READ,
})

_FACULTY = _STUDENT | frozenset({
    Capability.VIEW_SOURCE_METADATA,
    Capability.RUN_BRIEFING,
    Capability.VIEW_BRIEFING_HISTORY,
    Capability.STUDENTS_READ,
    Capability.ATTENDANCE_READ,
    Capability.EXAMS_READ,
    Capability.REPORTS_GENERATE,
    Capability.INTELLIGENCE_READ,
})

_STAFF = _STUDENT | frozenset({
    Capability.VIEW_SOURCE_METADATA,
    Capability.DATA_INGEST,
    Capability.DATA_REVIEW,
    Capability.STUDENTS_READ,
    Capability.FEES_READ,
    Capability.REPORTS_GENERATE,
    Capability.ACTIONS_NOTIFY,
})

_HOD = _FACULTY | frozenset({
    Capability.STUDENTS_READ_CONTACT,
    Capability.FEES_READ,
    Capability.FACULTY_READ,
    Capability.ACTIONS_EMAIL,
    Capability.ACTIONS_NOTIFY,
})

_PRINCIPAL = _HOD | _STAFF | frozenset({
    Capability.DOCUMENTS_MANAGE,
    Capability.RECORDS_WRITE,
    Capability.INTELLIGENCE_MANAGE,
})

_ALL = frozenset(Capability)

ROLE_CAPABILITIES: dict[PrincipalType, frozenset[Capability]] = {
    PrincipalType.ANONYMOUS: frozenset(),
    PrincipalType.STUDENT: _STUDENT,
    PrincipalType.FACULTY: _FACULTY,
    PrincipalType.STAFF: _STAFF,
    PrincipalType.HOD: _HOD,
    PrincipalType.PRINCIPAL: _PRINCIPAL,
    PrincipalType.INSTITUTION_ADMIN: _ALL,
    PrincipalType.MAIN_ADMIN: _ALL,
    PrincipalType.PLATFORM_SUPER_ADMIN: _ALL,
    PrincipalType.SYSTEM: _ALL,
}

# Aliases accepted from identity sources. Unknown strings never escalate; the
# caller decides which least-privilege default applies.
ROLE_ALIASES: dict[str, PrincipalType] = {
    "student": PrincipalType.STUDENT,
    "faculty": PrincipalType.FACULTY,
    "teacher": PrincipalType.FACULTY,
    "staff": PrincipalType.STAFF,
    "office_staff": PrincipalType.STAFF,
    "hod": PrincipalType.HOD,
    "head_of_department": PrincipalType.HOD,
    "principal": PrincipalType.PRINCIPAL,
    "institution_admin": PrincipalType.INSTITUTION_ADMIN,
    "main_admin": PrincipalType.MAIN_ADMIN,
    "admin": PrincipalType.MAIN_ADMIN,
    "platform_super_admin": PrincipalType.PLATFORM_SUPER_ADMIN,
    "super_admin": PrincipalType.PLATFORM_SUPER_ADMIN,
    "system": PrincipalType.SYSTEM,
}


def capabilities_for_role(role: PrincipalType) -> frozenset[Capability]:
    """Return the default capability grant for a verified role."""

    return ROLE_CAPABILITIES.get(role, frozenset())


def role_from_alias(value: object, default: PrincipalType = PrincipalType.STUDENT) -> PrincipalType:
    """Resolve a role string from an identity source without escalation."""

    if not isinstance(value, str):
        return default
    return ROLE_ALIASES.get(value.strip().lower(), default)


__all__ = ["ROLE_ALIASES", "ROLE_CAPABILITIES", "capabilities_for_role", "role_from_alias"]
