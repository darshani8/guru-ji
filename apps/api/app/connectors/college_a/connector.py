"""Deterministic College A adapter used for local development only."""

from __future__ import annotations

from datetime import datetime, timezone

from ...domain.provenance import Provenance, SourceKind, Warning
from ...domain.results import ResultStatus, ToolResult
from ...domain.source_health import SourceHealth
from ..base import ConnectorContext
from ..common.health import demo_health
from ..common.limits import bounded_int, ensure_within_limits


class CollegeADemoConnector:
    source_id = "college_a_demo"
    institution_id = "college_a"
    display_name = "College A (local demo)"

    async def health(self) -> SourceHealth:
        return demo_health(self.source_id, self.institution_id, self.display_name, "in_memory_demo")

    async def execute(self, tool_name: str, arguments: dict[str, object], context: ConnectorContext) -> ToolResult:
        if context.source_id != self.source_id:
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("source_mismatch", "connector context did not match the connector source", self.source_id),
            ))
        if tool_name == "institution.overview":
            data: object = {
                "institution_id": self.institution_id,
                "institution_name": "College A (demo data)",
                "active_students": 1240,
                "active_faculty": 86,
                "departments": 8,
                "attendance_rate_percent": 87.4,
            }
        elif tool_name == "institution.attendance_summary":
            requested_rows = bounded_int(arguments, "limit", 3, context.limits.max_rows)
            ensure_within_limits(context, requested_rows)
            departments = [
                {"department_id": "dept-cse", "name": "Computer Science", "attendance_rate_percent": 89.1},
                {"department_id": "dept-ece", "name": "Electronics", "attendance_rate_percent": 85.8},
                {"department_id": "dept-me", "name": "Mechanical", "attendance_rate_percent": 83.6},
            ]
            data = {
                "period": "current_demo_term",
                "attendance_rate_percent": 87.4,
                "at_risk_cohort_count": 3,
                "departments": departments[:requested_rows or len(departments)],
            }
        elif tool_name == "institution.source_health":
            health = await self.health()
            data = {
                "source_id": health.source_id,
                "institution_id": health.institution_id,
                "status": health.status.value,
                "freshness": health.freshness.value,
                "detail": health.detail,
            }
        else:
            return ToolResult(tool_name=tool_name, status=ResultStatus.UNAVAILABLE, warnings=(
                Warning("unsupported_tool", f"demo connector does not implement {tool_name}", self.source_id),
            ))
        provenance = Provenance(
            source_id=self.source_id,
            source_type=SourceKind.CONTROL_PLANE,
            complete=True,
            rows_used=1,
            retrieved_at=datetime.now(timezone.utc),
        )
        return ToolResult(tool_name=tool_name, status=ResultStatus.SUCCESS, data=data, provenance=(provenance,), warnings=(
            Warning("demo_data", "This result is deterministic demo data and did not contact a college system", self.source_id),
        ))


__all__ = ["CollegeADemoConnector"]
