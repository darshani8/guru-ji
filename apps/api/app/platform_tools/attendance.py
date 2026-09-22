"""Attendance tools."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import describe_filters, output
from .context import PlatformServices


def build_attendance_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data
    common = (
        param("program", "string", "Program code such as MBA", max_length=60),
        param("semester", "string", "Semester number", max_length=20),
        param("department", "string", "Department", max_length=120),
        param("period", "string", "Reporting period label (month/term) as recorded", max_length=60),
    )

    async def find_low_attendance(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.low_attendance(
            context.principal, context.institution_id, threshold=args.get("threshold", 75.0), program=args.get("program"), semester=args.get("semester"),
            department=args.get("department"), period=args.get("period"), limit=args.get("limit"), include_students=args.get("include_students", True),
        )
        summary = f"{result['count']} of {result['students_evaluated']} student(s){describe_filters(result['filters'])} are below {result['threshold']:g}% attendance."
        warnings = []
        if result["students_without_attendance_data"]:
            warnings.append({"code": "attendance_unknown", "message": f"{result['students_without_attendance_data']} student(s) had attendance rows without usable counts or percentages."})
        if result["students_evaluated"] == 0:
            warnings.append({"code": "no_attendance_data", "message": "No attendance records match the filters; import attendance data first."})
        returned = result.get("returned", 0)
        return output(context.institution_id, "attendance", result, summary, rows_used=result["students_evaluated"], records_returned=returned, warnings=warnings, complete=returned >= result["count"] or not args.get("include_students", True))

    async def get_attendance_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.attendance_summary(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), department=args.get("department"), period=args.get("period"))
        if result["students_evaluated"] == 0:
            return output(context.institution_id, "attendance", result, "No attendance records match the filters.", rows_used=0, warnings=[{"code": "no_attendance_data", "message": "Import attendance data first."}])
        summary = f"Average attendance{describe_filters(result['filters'])} is {result['average_percent']}% across {result['students_evaluated']} student(s); {result['below_75_percent']} are below 75%."
        return output(context.institution_id, "attendance", result, summary, rows_used=result["students_evaluated"], records_returned=0)

    return (
        PlatformToolSpec(
            name="find_low_attendance", group="attendance", description="Find students whose attendance is below a threshold percentage. Returns the count and a bounded list.",
            required_capability=Capability.ATTENDANCE_READ, handler=find_low_attendance, risk=RiskLevel.READ, returns="{count, students_evaluated, students[]}",
            parameters=(param("threshold", "number", "Attendance percentage threshold", minimum=1, maximum=100, default=75), *common, param("limit", "integer", "Maximum students to list", minimum=1, maximum=500, default=100), param("include_students", "boolean", "Return the student list, not only the count", default=True)),
            examples=("How many MBA students have attendance below 75%?", "List students under 65% attendance in semester 3"),
        ),
        PlatformToolSpec(
            name="get_attendance_summary", group="attendance", description="Aggregate attendance: average, counts below 75% and 65%, by program.",
            required_capability=Capability.ATTENDANCE_READ, handler=get_attendance_summary, risk=RiskLevel.READ, returns="{average_percent, below_75_percent, by_program}",
            parameters=common,
        ),
    )


__all__ = ["build_attendance_tools"]
