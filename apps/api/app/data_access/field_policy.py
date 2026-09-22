"""Data minimization: which fields a caller may receive for each entity."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..domain.principals import Capability, Principal
from ..normalization.canonical import CANONICAL_ENTITIES

STUDENT_BASE_FIELDS = ("student_id", "name", "program", "department", "semester", "section", "batch", "status")
STUDENT_CONTACT_FIELDS = ("phone", "email", "guardian_name", "guardian_phone", "address", "date_of_birth", "gender", "category", "blood_group", "admission_year")
FACULTY_BASE_FIELDS = ("faculty_id", "name", "designation", "department", "qualification", "specialization", "is_hod", "status")
FACULTY_CONTACT_FIELDS = ("email", "phone", "joining_date", "experience_years")
STAFF_BASE_FIELDS = ("staff_id", "name", "designation", "department", "status")
STAFF_CONTACT_FIELDS = ("email", "phone", "joining_date")
SENSITIVE_STUDENT_KEYS = frozenset(STUDENT_CONTACT_FIELDS)


def allowed_student_fields(principal: Principal) -> tuple[str, ...]:
    fields = list(STUDENT_BASE_FIELDS)
    if principal.has_capability(Capability.STUDENTS_READ_CONTACT):
        fields.extend(STUDENT_CONTACT_FIELDS)
    return tuple(fields)


def allowed_faculty_fields(principal: Principal) -> tuple[str, ...]:
    fields = list(FACULTY_BASE_FIELDS)
    if principal.has_capability(Capability.FACULTY_READ):
        fields.extend(FACULTY_CONTACT_FIELDS)
    return tuple(fields)


def allowed_staff_fields(principal: Principal) -> tuple[str, ...]:
    fields = list(STAFF_BASE_FIELDS)
    if principal.has_capability(Capability.FACULTY_READ):
        fields.extend(STAFF_CONTACT_FIELDS)
    return tuple(fields)


def minimize(record: Mapping[str, Any], allowed: Sequence[str], requested: Iterable[str] | None = None) -> dict[str, Any]:
    """Return only requested-and-allowed fields; unknown or disallowed names are dropped silently."""

    wanted = [name for name in (requested or allowed) if name in allowed]
    return {name: record.get(name) for name in wanted}


def strip_student_contact(value: Any, principal: Principal) -> Any:
    """Second, generic pass applied by the gateway to any tool output."""

    if principal.has_capability(Capability.STUDENTS_READ_CONTACT):
        return value
    if isinstance(value, Mapping):
        if "student_id" in value:
            return {key: strip_student_contact(item, principal) for key, item in value.items() if key not in SENSITIVE_STUDENT_KEYS}
        return {key: strip_student_contact(item, principal) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_student_contact(item, principal) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_student_contact(item, principal) for item in value)
    return value


def entity_fields(entity: str) -> tuple[str, ...]:
    return CANONICAL_ENTITIES[entity].field_names()


__all__ = [
    "FACULTY_BASE_FIELDS", "FACULTY_CONTACT_FIELDS", "STUDENT_BASE_FIELDS", "STUDENT_CONTACT_FIELDS",
    "allowed_faculty_fields", "allowed_staff_fields", "allowed_student_fields", "entity_fields", "minimize", "strip_student_contact",
]
