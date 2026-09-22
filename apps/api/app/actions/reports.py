"""Generate downloadable reports and keep them in object storage with metadata."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..data_access.field_policy import SENSITIVE_STUDENT_KEYS, strip_student_contact
from ..domain.principals import Capability, InstitutionScope, Principal
from ..institution_data.store import InstitutionDataStore
from ..storage.object_store import ObjectStore, build_object_key
from .files import MAX_REPORT_ROWS, render_report


# Report bytes are whatever rows the generating principal supplied, and the
# planner labels columns freely, so header text alone cannot prove a report
# carries no sensitive data. A report therefore requires every data capability
# its creator held (anything the creator could see may be in it), widened by
# the capabilities its canonical column names imply. The requirement is stored
# on the report record; a record without one is visible to its creator only.
DATA_CAPABILITIES: tuple[Capability, ...] = (Capability.STUDENTS_READ_CONTACT, Capability.ATTENDANCE_READ, Capability.FEES_READ, Capability.EXAMS_READ)
ATTENDANCE_COLUMNS = frozenset({"attendance_percent", "classes_held", "classes_attended", "attendance", "present", "absent", "period"})
FEE_COLUMNS = frozenset({"balance", "amount_due", "amount_paid", "earliest_due_date", "fee_type", "due_date", "payment_status", "receipt_number", "paid_on"})
EXAM_COLUMNS = frozenset({"marks_obtained", "max_marks", "grade", "result_status", "exam_name", "pass_rate_percent", "exam_date"})
_COLUMN_CAPABILITIES: tuple[tuple[frozenset[str], Capability], ...] = (
    (SENSITIVE_STUDENT_KEYS, Capability.STUDENTS_READ_CONTACT),
    (ATTENDANCE_COLUMNS, Capability.ATTENDANCE_READ),
    (FEE_COLUMNS, Capability.FEES_READ),
    (EXAM_COLUMNS, Capability.EXAMS_READ),
)


def required_capabilities_for(columns: Iterable[str]) -> tuple[str, ...]:
    """Capabilities a reader needs for a report whose rows carry these columns."""

    present = {str(column).strip().lower() for column in columns}
    required = [capability.value for names, capability in _COLUMN_CAPABILITIES if present & names]
    return tuple(sorted(required))


@dataclass(slots=True)
class ReportService:
    store: InstitutionDataStore
    objects: ObjectStore

    @staticmethod
    def _file_name(title: str, fmt: str) -> str:
        base = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()[:60] or "report"
        return f"{base}.{fmt}"

    def generate(self, principal: Principal, institution_id: str, *, title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], format_name: str = "xlsx", tool_name: str = "generate_report", subtitle: str | None = None) -> dict[str, Any]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("report scope is outside the caller's institution")
        if not principal.has_capability(Capability.REPORTS_GENERATE):
            raise PermissionError("reports:generate capability is required")
        if not title.strip():
            raise ValueError("report title must not be blank")
        if not columns:
            raise ValueError("report needs at least one column")
        if len(rows) > MAX_REPORT_ROWS:
            raise ValueError(f"report exceeds {MAX_REPORT_ROWS} rows")
        fmt = format_name.lower().strip()
        if fmt == "excel":
            fmt = "xlsx"
        # The same minimisation the gateway applies to tool output: a principal
        # without students:read_contact never gets contact fields into a file.
        minimized_rows: list[Mapping[str, Any]] = strip_student_contact([dict(row) for row in rows], principal)
        minimized_columns = list(columns)
        if not principal.has_capability(Capability.STUDENTS_READ_CONTACT) and any("student_id" in row for row in rows):
            minimized_columns = [column for column in minimized_columns if str(column).strip().lower() not in SENSITIVE_STUDENT_KEYS]
            if not minimized_columns:
                raise ValueError("report needs at least one column you are permitted to export")
        present = set(minimized_columns) | {key for row in minimized_rows for key in row}
        required_capabilities = self.required_capabilities_for_creator(principal, present)
        content, content_type = render_report(fmt, title.strip(), minimized_columns, list(minimized_rows), subtitle=subtitle)
        report_id = f"rpt-{uuid4().hex}"
        file_name = self._file_name(title, fmt)
        key = build_object_key(institution_id, "reports", report_id, file_name)
        self.objects.put(key, content, content_type)
        record = self.store.add_report(
            institution_id, report_id=report_id, title=title.strip(), format_name=fmt, object_key=key, size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), row_count=len(rows), tool_name=tool_name, created_by=principal.principal_id,
            required_capabilities=required_capabilities,
        )
        return {
            "report_id": report_id, "title": record.get("title"), "format": fmt, "file_name": file_name, "row_count": len(rows),
            "size_bytes": len(content), "content_type": content_type, "download_path": f"/v1/reports/{report_id}/download",
            "created_at": record.get("created_at"),
        }

    @staticmethod
    def required_capabilities_for_creator(principal: Principal, columns: Iterable[str]) -> tuple[str, ...]:
        """Every data capability the creator holds, plus those the canonical column names imply."""

        held = {capability.value for capability in DATA_CAPABILITIES if principal.has_capability(capability)}
        return tuple(sorted(held | set(required_capabilities_for(columns))))

    @staticmethod
    def required_capabilities(record: Mapping[str, Any]) -> tuple[str, ...] | None:
        """The capabilities recorded at generation time, or None when unknown (deny to non-creators)."""

        required = record.get("required_capabilities")
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return None
        return tuple(required)

    def _readable(self, principal: Principal, record: Mapping[str, Any]) -> bool:
        """The creator always sees their own report; anyone else needs reports:generate plus every recorded capability."""

        if record.get("created_by") == principal.principal_id:
            return True
        if not principal.has_capability(Capability.REPORTS_GENERATE):
            return False
        required = self.required_capabilities(record)
        if required is None:
            return False
        known = {capability.value: capability for capability in Capability}
        return all(value in known and principal.has_capability(known[value]) for value in required)

    def fetch(self, principal: Principal, institution_id: str, report_id: str) -> tuple[dict[str, Any], bytes]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("report scope is outside the caller's institution")
        record = self.store.get_report(institution_id, report_id)
        if record is None:
            raise KeyError(f"report not found: {report_id}")
        if not self._readable(principal, record):
            raise PermissionError("you do not hold every permission the data in another user's report requires")
        return record, self.objects.get(record["object_key"])

    def list(self, principal: Principal, institution_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("report scope is outside the caller's institution")
        rows = [row for row in self.store.list_reports(institution_id, limit=limit) if self._readable(principal, row)]
        return [{key: row.get(key) for key in ("report_id", "title", "format", "row_count", "size_bytes", "created_by", "created_at", "tool_name")} | {"download_path": f"/v1/reports/{row['report_id']}/download"} for row in rows]


__all__ = ["DATA_CAPABILITIES", "ReportService", "required_capabilities_for"]
