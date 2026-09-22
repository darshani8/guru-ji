"""Generate downloadable reports and keep them in object storage with metadata."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..domain.principals import Capability, InstitutionScope, Principal
from ..institution_data.store import InstitutionDataStore
from ..storage.object_store import ObjectStore, build_object_key
from .files import MAX_REPORT_ROWS, render_report


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
        content, content_type = render_report(fmt, title.strip(), list(columns), list(rows), subtitle=subtitle)
        report_id = f"rpt-{uuid4().hex}"
        file_name = self._file_name(title, fmt)
        key = build_object_key(institution_id, "reports", report_id, file_name)
        self.objects.put(key, content, content_type)
        record = self.store.add_report(
            institution_id, report_id=report_id, title=title.strip(), format_name=fmt, object_key=key, size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), row_count=len(rows), tool_name=tool_name, created_by=principal.principal_id,
        )
        return {
            "report_id": report_id, "title": record.get("title"), "format": fmt, "file_name": file_name, "row_count": len(rows),
            "size_bytes": len(content), "content_type": content_type, "download_path": f"/v1/reports/{report_id}/download",
            "created_at": record.get("created_at"),
        }

    def fetch(self, principal: Principal, institution_id: str, report_id: str) -> tuple[dict[str, Any], bytes]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("report scope is outside the caller's institution")
        record = self.store.get_report(institution_id, report_id)
        if record is None:
            raise KeyError(f"report not found: {report_id}")
        if record["created_by"] != principal.principal_id and not principal.has_capability(Capability.REPORTS_GENERATE):
            raise PermissionError("reports:generate capability is required to download another user's report")
        return record, self.objects.get(record["object_key"])

    def list(self, principal: Principal, institution_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("report scope is outside the caller's institution")
        rows = self.store.list_reports(institution_id, limit=limit)
        if not principal.has_capability(Capability.REPORTS_GENERATE):
            rows = [row for row in rows if row["created_by"] == principal.principal_id]
        return [{key: row.get(key) for key in ("report_id", "title", "format", "row_count", "size_bytes", "created_by", "created_at", "tool_name")} | {"download_path": f"/v1/reports/{row['report_id']}/download"} for row in rows]


__all__ = ["ReportService"]
