"""Domain-specific data access.

Each method answers one institutional question with bounded, minimized data.
The caller's principal and institution are checked on every call, so neither
the agent nor a route can widen the scope. There is no method that accepts a
query string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..domain.principals import Capability, InstitutionScope, Principal
from ..institution_data.store import InstitutionDataStore
from ..normalization.canonical import PROGRAM_ALIASES
from ..normalization.cleaning import normalize_email, normalize_phone, normalize_program, normalize_semester
from .field_policy import allowed_faculty_fields, allowed_staff_fields, allowed_student_fields, minimize

MAX_LIST = 500
STUDENT_UPDATABLE_FIELDS = frozenset({"semester", "section", "status", "phone", "email", "address", "guardian_name", "guardian_phone", "department", "program", "batch"})
_STUDENT_PHONE_FIELDS = frozenset({"phone", "guardian_phone"})
_STUDENT_EMAIL_FIELDS = frozenset({"email"})
_PHONE_PATTERN = re.compile(r"^\+?\d{7,15}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class DataAccessDenied(PermissionError):
    """Raised when the principal may not access the requested institution or data."""


@dataclass(slots=True)
class InstitutionDataService:
    store: InstitutionDataStore

    # ------------------------------------------------------------- guards
    def _guard(self, principal: Principal, institution_id: str, capability: Capability | None = None) -> None:
        if not principal.active:
            raise DataAccessDenied("authentication is required")
        if not principal.can_access(InstitutionScope(institution_id)):
            raise DataAccessDenied("the requested institution is outside the caller's scope")
        if capability is not None and not principal.has_capability(capability):
            raise DataAccessDenied(f"{capability.value} capability is required")

    @staticmethod
    def _limit(limit: int | None) -> int:
        if limit is None:
            return 100
        return max(1, min(int(limit), MAX_LIST))

    # --------------------------------------------------------- vocabulary
    def known_programs(self, institution_id: str) -> list[str]:
        values = self.store.distinct_values(institution_id, "program", "code") + self.store.distinct_values(institution_id, "student", "program")
        # Skip bare numbers: they come from a mis-mapped marks column, not a program.
        return sorted({value.upper() for value in values if value and not re.fullmatch(r"-?\d+(\.\d+)?%?", str(value).strip())})

    def known_departments(self, institution_id: str) -> list[str]:
        values = self.store.distinct_values(institution_id, "department", "name") + self.store.distinct_values(institution_id, "student", "department") + self.store.distinct_values(institution_id, "faculty", "department")
        return sorted({value for value in values if value}, key=str.lower)

    def resolve_program(self, institution_id: str, text: str | None) -> str | None:
        if not text:
            return None
        normalized, _ = normalize_program(text)
        known = self.known_programs(institution_id)
        if normalized in known or not known:
            return normalized
        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        for candidate in known:
            if re.sub(r"[^a-z0-9]", "", candidate.lower()) == compact or PROGRAM_ALIASES.get(compact) == candidate:
                return candidate
        return normalized

    @staticmethod
    def resolve_semester(value: Any) -> int | None:
        if value in (None, ""):
            return None
        semester, _ = normalize_semester(value)
        return semester

    # ----------------------------------------------------------- summary
    def institution_summary(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        counts = self.store.entity_counts(institution_id)
        institution = self.store.get_institution(institution_id) or {"institution_id": institution_id, "name": institution_id}
        summary: dict[str, Any] = {
            "institution_id": institution_id,
            "institution_name": institution.get("name"),
            "location": institution.get("location"),
            "record_counts": counts,
            "programs": self.known_programs(institution_id)[:50],
            "departments": self.known_departments(institution_id)[:50],
        }
        # Each indicator block is gated by the same capability as the dedicated
        # tool that exposes it; the overview never widens what a caller may see.
        if counts.get("attendance") and principal.has_capability(Capability.ATTENDANCE_READ):
            rollup = self.store.attendance_rollup(institution_id)
            percents = [item["attendance_percent"] for item in rollup if item["attendance_percent"] is not None]
            summary["attendance"] = {
                "students_with_records": len(rollup),
                "average_percent": round(sum(percents) / len(percents), 2) if percents else None,
                "below_75_percent": sum(1 for value in percents if value < 75),
            }
        if counts.get("fee") and principal.has_capability(Capability.FEES_READ):
            fees = self.store.fee_rollup(institution_id)
            summary["fees"] = {
                "students_with_dues": sum(1 for item in fees if item["balance"] > 0),
                "total_outstanding": round(sum(item["balance"] for item in fees if item["balance"] > 0), 2),
            }
        if counts.get("admission") and principal.has_capability(Capability.ASK_READ_ONLY):
            summary["admissions"] = self.admissions_summary(principal, institution_id)["by_status"]
        if counts.get("event") and principal.has_capability(Capability.ASK_READ_ONLY):
            summary["upcoming_events"] = len(self.upcoming_events(principal, institution_id, days=30)["events"])
        return summary

    # ----------------------------------------------------------- students
    def _student_filters(self, institution_id: str, program: str | None, semester: Any, department: str | None, section: str | None = None, status: str | None = None) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        resolved_program = self.resolve_program(institution_id, program)
        if resolved_program:
            filters["program"] = resolved_program
        resolved_semester = self.resolve_semester(semester)
        if resolved_semester is not None:
            filters["semester"] = resolved_semester
        if department:
            filters["department"] = department.strip()
        if section:
            filters["section"] = section.strip().upper()
        if status:
            filters["status"] = status.strip().lower()
        return filters

    def count_students(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, department: str | None = None, status: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.STUDENTS_READ)
        filters = self._student_filters(institution_id, program, semester, department, status=status)
        return {"count": self.store.count_records(institution_id, "student", filters), "filters": filters}

    def find_students(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, department: str | None = None, section: str | None = None, name_contains: str | None = None, status: str | None = None, limit: int | None = None, fields: Iterable[str] | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.STUDENTS_READ)
        filters = self._student_filters(institution_id, program, semester, department, section, status)
        if name_contains:
            filters["name__contains"] = name_contains.strip()
        allowed = allowed_student_fields(principal)
        rows = self.store.query_records(institution_id, "student", filters, limit=self._limit(limit), order_by="student_id")
        total = self.store.count_records(institution_id, "student", filters)
        return {"count": total, "returned": len(rows), "filters": filters, "students": [minimize(row, allowed, fields) for row in rows], "fields": list(fields or allowed)}

    def get_student(self, principal: Principal, institution_id: str, student_id: str, *, fields: Iterable[str] | None = None) -> dict[str, Any] | None:
        self._guard(principal, institution_id, Capability.STUDENTS_READ)
        record = self.store.get_record(institution_id, "student", student_id.strip().lower())
        if record is None:
            return None
        allowed = allowed_student_fields(principal)
        result = minimize(record, allowed, fields)
        result["lineage"] = record.get("lineage")
        return result

    def validate_student_changes(self, institution_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize a student update before it is approved or applied.

        Every value must be a scalar; text fields are stored as stripped
        strings so the SQL parameters always match the TEXT columns on
        PostgreSQL. The result is idempotent: validating it again yields the
        same mapping, so an approval digest computed on it stays stable.
        """

        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("changes must name at least one field to update")
        cleaned: dict[str, Any] = {}
        for raw_key, value in changes.items():
            key = str(raw_key).strip()
            if key not in STUDENT_UPDATABLE_FIELDS:
                raise ValueError(f"field cannot be updated through the assistant: {key}")
            if value is None:
                raise ValueError(f"{key} must have a value; clearing a field is not supported through the assistant")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"{key} must be a single text or numeric value, not a nested object or list")
            if key == "semester":
                value = self.resolve_semester(value)
                if value is None:
                    raise ValueError("semester must be a number between 1 and 20")
            elif key == "program":
                text = str(value).strip()
                if not text:
                    raise ValueError("program must not be blank")
                value = self.resolve_program(institution_id, text)
            elif key in _STUDENT_PHONE_FIELDS:
                digits, _ = normalize_phone(value)
                if not _PHONE_PATTERN.fullmatch(digits):
                    raise ValueError(f"{key} must be a phone number of 7 to 15 digits, optionally starting with +")
                value = digits
            elif key in _STUDENT_EMAIL_FIELDS:
                text, _ = normalize_email(value)
                if len(text) > 254 or not _EMAIL_PATTERN.fullmatch(text):
                    raise ValueError(f"{key} must be a valid email address")
                value = text
            else:
                value = str(value).strip()
                if not value:
                    raise ValueError(f"{key} must not be blank")
                if len(value) > 500:
                    raise ValueError(f"{key} exceeds 500 characters")
            cleaned[key] = value
        return cleaned

    def update_student(self, principal: Principal, institution_id: str, student_id: str, changes: Mapping[str, Any], *, locator: str) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.RECORDS_WRITE)
        cleaned = self.validate_student_changes(institution_id, changes)
        before, after = self.store.update_record_fields(institution_id, "student", student_id.strip().lower(), cleaned, locator=locator)
        allowed = allowed_student_fields(principal)
        return {"student_id": after.get("student_id"), "changed_fields": sorted(cleaned), "before": minimize(before, allowed, cleaned.keys()), "after": minimize(after, allowed, cleaned.keys())}

    # --------------------------------------------------------- attendance
    def low_attendance(self, principal: Principal, institution_id: str, *, threshold: float = 75.0, program: str | None = None, semester: Any = None, department: str | None = None, period: str | None = None, limit: int | None = None, include_students: bool = True) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ATTENDANCE_READ)
        if not 0 < float(threshold) <= 100:
            raise ValueError("threshold must be between 0 and 100")
        rollup = self.store.attendance_rollup(institution_id, program=self.resolve_program(institution_id, program), semester=self.resolve_semester(semester), department=department, period=period)
        below = [item for item in rollup if item["attendance_percent"] is not None and item["attendance_percent"] < float(threshold)]
        below.sort(key=lambda item: (item["attendance_percent"], item["student_id"]))
        unknown = sum(1 for item in rollup if item["attendance_percent"] is None)
        result: dict[str, Any] = {
            "count": len(below), "students_evaluated": len(rollup), "threshold": float(threshold),
            "filters": {"program": self.resolve_program(institution_id, program), "semester": self.resolve_semester(semester), "department": department, "period": period},
            "students_without_attendance_data": unknown,
        }
        if include_students:
            result["students"] = [
                {"student_id": item["student_id"], "name": item["name"], "program": item["program"], "semester": item["semester"], "attendance_percent": item["attendance_percent"], "classes_held": item["classes_held"], "classes_attended": item["classes_attended"]}
                for item in below[: self._limit(limit)]
            ]
            result["returned"] = len(result["students"])
        return result

    def attendance_summary(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, department: str | None = None, period: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ATTENDANCE_READ)
        rollup = self.store.attendance_rollup(institution_id, program=self.resolve_program(institution_id, program), semester=self.resolve_semester(semester), department=department, period=period)
        percents = [item["attendance_percent"] for item in rollup if item["attendance_percent"] is not None]
        by_program: dict[str, list[float]] = {}
        for item in rollup:
            if item["attendance_percent"] is not None:
                by_program.setdefault(str(item["program"] or "unknown"), []).append(item["attendance_percent"])
        return {
            "students_evaluated": len(rollup),
            "average_percent": round(sum(percents) / len(percents), 2) if percents else None,
            "below_75_percent": sum(1 for value in percents if value < 75),
            "below_65_percent": sum(1 for value in percents if value < 65),
            "by_program": {name: {"students": len(values), "average_percent": round(sum(values) / len(values), 2)} for name, values in sorted(by_program.items())},
            "filters": {"program": self.resolve_program(institution_id, program), "semester": self.resolve_semester(semester), "department": department, "period": period},
        }

    # --------------------------------------------------------------- fees
    def pending_fees(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, academic_year: str | None = None, limit: int | None = None, include_students: bool = True) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.FEES_READ)
        rollup = self.store.fee_rollup(institution_id, program=self.resolve_program(institution_id, program), semester=self.resolve_semester(semester), academic_year=academic_year)
        pending = [item for item in rollup if item["balance"] > 0]
        result: dict[str, Any] = {
            "count": len(pending), "total_outstanding": round(sum(item["balance"] for item in pending), 2), "students_evaluated": len(rollup),
            "filters": {"program": self.resolve_program(institution_id, program), "semester": self.resolve_semester(semester), "academic_year": academic_year},
        }
        if include_students:
            result["students"] = [
                {"student_id": item["student_id"], "name": item["name"], "program": item["program"], "semester": item["semester"], "balance": item["balance"], "amount_due": item["amount_due"], "amount_paid": item["amount_paid"], "earliest_due_date": item["earliest_due_date"]}
                for item in pending[: self._limit(limit)]
            ]
            result["returned"] = len(result["students"])
        return result

    def fee_summary(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, academic_year: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.FEES_READ)
        rollup = self.store.fee_rollup(institution_id, program=self.resolve_program(institution_id, program), semester=self.resolve_semester(semester), academic_year=academic_year)
        due = round(sum(item["amount_due"] for item in rollup), 2)
        paid = round(sum(item["amount_paid"] for item in rollup), 2)
        return {
            "students_evaluated": len(rollup), "total_due": due, "total_paid": paid, "total_outstanding": round(sum(item["balance"] for item in rollup if item["balance"] > 0), 2),
            "collection_percent": round(paid / due * 100, 2) if due else None, "students_with_dues": sum(1 for item in rollup if item["balance"] > 0),
            "filters": {"program": self.resolve_program(institution_id, program), "semester": self.resolve_semester(semester), "academic_year": academic_year},
        }

    # -------------------------------------------------------------- exams
    def exam_summary(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, course_code: str | None = None, exam_name: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.EXAMS_READ)
        rows = self.store.exam_rollup(institution_id, program=self.resolve_program(institution_id, program), semester=self.resolve_semester(semester), course_code=course_code, exam_name=exam_name)
        students = sum(item["students"] for item in rows)
        passed = sum(item["passed"] for item in rows)
        failed = sum(item["failed"] for item in rows)
        return {
            "results": students, "passed": passed, "failed": failed, "pass_rate_percent": round(passed / (passed + failed) * 100, 2) if (passed + failed) else None,
            "courses": rows[:100], "filters": {"program": self.resolve_program(institution_id, program), "semester": self.resolve_semester(semester), "course_code": course_code, "exam_name": exam_name},
        }

    def student_results(self, principal: Principal, institution_id: str, student_id: str, *, limit: int | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.EXAMS_READ)
        rows = self.store.query_records(institution_id, "exam", {"student_id": student_id.strip()}, limit=self._limit(limit), order_by="semester")
        return {"student_id": student_id, "count": len(rows), "results": [{key: row.get(key) for key in ("course_code", "course_name", "exam_name", "semester", "marks_obtained", "max_marks", "grade", "result_status", "exam_date")} for row in rows]}

    # ------------------------------------------------------- programs etc
    def list_programs(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        rows = self.store.query_records(institution_id, "program", limit=MAX_LIST, order_by="code")
        programs = [{key: row.get(key) for key in ("code", "name", "department", "level", "duration_semesters", "intake")} for row in rows]
        known = set(item["code"] for item in programs if item["code"])
        for code in self.known_programs(institution_id):
            if code not in known:
                programs.append({"code": code, "name": None, "department": None, "level": None, "duration_semesters": None, "intake": None, "source": "derived_from_student_records"})
        return {"count": len(programs), "programs": programs}

    def list_departments(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        rows = self.store.query_records(institution_id, "department", limit=MAX_LIST, order_by="code")
        departments = [{key: row.get(key) for key in ("code", "name", "hod_id", "hod_name")} for row in rows]
        known = {str(item.get("name") or "").lower() for item in departments} | {str(item.get("code") or "").lower() for item in departments}
        for name in self.known_departments(institution_id):
            if name.lower() not in known:
                departments.append({"code": None, "name": name, "hod_id": None, "hod_name": None, "source": "derived_from_records"})
        return {"count": len(departments), "departments": departments}

    def list_courses(self, principal: Principal, institution_id: str, *, program: str | None = None, semester: Any = None, limit: int | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        filters: dict[str, Any] = {}
        resolved = self.resolve_program(institution_id, program)
        if resolved:
            filters["program"] = resolved
        sem = self.resolve_semester(semester)
        if sem is not None:
            filters["semester"] = sem
        rows = self.store.query_records(institution_id, "course", filters, limit=self._limit(limit), order_by="code")
        return {"count": len(rows), "courses": [{key: row.get(key) for key in ("code", "name", "program", "semester", "credits", "faculty_id", "course_type")} for row in rows]}

    # ------------------------------------------------------------ faculty
    def find_faculty(self, principal: Principal, institution_id: str, *, department: str | None = None, designation: str | None = None, name_contains: str | None = None, limit: int | None = None, fields: Iterable[str] | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.FACULTY_READ)
        filters: dict[str, Any] = {}
        if department:
            filters["department__contains"] = department.strip()
        if designation:
            filters["designation__contains"] = designation.strip()
        if name_contains:
            filters["name__contains"] = name_contains.strip()
        rows = self.store.query_records(institution_id, "faculty", filters, limit=self._limit(limit), order_by="name")
        allowed = allowed_faculty_fields(principal)
        return {"count": len(rows), "faculty": [minimize(row, allowed, fields) for row in rows]}

    def find_hod(self, principal: Principal, institution_id: str, *, department: str | None = None, program: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.FACULTY_READ)
        allowed = allowed_faculty_fields(principal)
        target = (department or "").strip()
        if not target and program:
            resolved = self.resolve_program(institution_id, program)
            program_rows = self.store.query_records(institution_id, "program", {"code": resolved} if resolved else None, limit=5)
            if program_rows and program_rows[0].get("department"):
                target = str(program_rows[0]["department"])
            else:
                target = resolved or ""
        candidates: list[dict[str, Any]] = []
        if target:
            dept_rows = self.store.query_records(institution_id, "department", {"name__contains": target}, limit=5) or self.store.query_records(institution_id, "department", {"code": target}, limit=5)
            for dept in dept_rows:
                if dept.get("hod_id"):
                    faculty = self.store.get_record(institution_id, "faculty", str(dept["hod_id"]).lower())
                    if faculty:
                        candidates.append(minimize(faculty, allowed))
                elif dept.get("hod_name"):
                    for faculty in self.store.query_records(institution_id, "faculty", {"name__contains": str(dept["hod_name"])}, limit=3):
                        candidates.append(minimize(faculty, allowed))
            if not candidates:
                for faculty in self.store.query_records(institution_id, "faculty", {"department__contains": target, "is_hod": True}, limit=5):
                    candidates.append(minimize(faculty, allowed))
            if not candidates:
                for faculty in self.store.query_records(institution_id, "faculty", {"department__contains": target, "designation__contains": "head"}, limit=5):
                    candidates.append(minimize(faculty, allowed))
        else:
            for faculty in self.store.query_records(institution_id, "faculty", {"is_hod": True}, limit=50):
                candidates.append(minimize(faculty, allowed))
        return {"department": target or None, "count": len(candidates), "hods": candidates}

    def find_staff(self, principal: Principal, institution_id: str, *, department: str | None = None, name_contains: str | None = None, limit: int | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.FACULTY_READ)
        filters: dict[str, Any] = {}
        if department:
            filters["department__contains"] = department.strip()
        if name_contains:
            filters["name__contains"] = name_contains.strip()
        rows = self.store.query_records(institution_id, "staff", filters, limit=self._limit(limit), order_by="name")
        return {"count": len(rows), "staff": [minimize(row, allowed_staff_fields(principal)) for row in rows]}

    # ------------------------------------------------- events & admissions
    def upcoming_events(self, principal: Principal, institution_id: str, *, days: int = 30, limit: int | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        today = date.today()
        horizon = today + timedelta(days=max(1, min(int(days), 365)))
        rows = self.store.query_records(institution_id, "event", {"event_date__gte": today.isoformat(), "event_date__lte": horizon.isoformat()}, limit=self._limit(limit), order_by="event_date")
        return {"from": today.isoformat(), "to": horizon.isoformat(), "count": len(rows), "events": [{key: row.get(key) for key in ("title", "event_date", "end_date", "category", "organizer", "venue")} for row in rows]}

    def admissions_summary(self, principal: Principal, institution_id: str, *, program: str | None = None, academic_year: str | None = None) -> dict[str, Any]:
        self._guard(principal, institution_id, Capability.ASK_READ_ONLY)
        filters: dict[str, Any] = {}
        resolved = self.resolve_program(institution_id, program)
        if resolved:
            filters["program"] = resolved
        if academic_year:
            filters["academic_year"] = academic_year
        rows = self.store.query_records(institution_id, "admission", filters, limit=5000)
        by_status: dict[str, int] = {}
        by_program: dict[str, int] = {}
        for row in rows:
            by_status[str(row.get("status") or "unknown").lower()] = by_status.get(str(row.get("status") or "unknown").lower(), 0) + 1
            by_program[str(row.get("program") or "unknown")] = by_program.get(str(row.get("program") or "unknown"), 0) + 1
        return {"applications": len(rows), "by_status": by_status, "by_program": by_program, "filters": filters}


__all__ = ["DataAccessDenied", "InstitutionDataService", "STUDENT_UPDATABLE_FIELDS"]
