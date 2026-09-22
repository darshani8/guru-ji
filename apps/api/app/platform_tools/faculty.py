"""Faculty, staff, and HOD lookup tools."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import output
from .context import PlatformServices


def build_faculty_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data

    async def find_faculty(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.find_faculty(context.principal, context.institution_id, department=args.get("department"), designation=args.get("designation"), name_contains=args.get("name_contains"), limit=args.get("limit"), fields=args.get("fields"))
        return output(context.institution_id, "faculty", result, f"{result['count']} faculty member(s) matched.", rows_used=result["count"])

    async def find_hod(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.find_hod(context.principal, context.institution_id, department=args.get("department"), program=args.get("program"))
        if not result["count"]:
            return output(context.institution_id, "faculty", result, f"No head of department was found for {result.get('department') or 'the requested unit'}.", rows_used=0, warnings=[{"code": "hod_not_found", "message": "Import department or faculty data with an HOD marker to resolve heads of department."}])
        names = ", ".join(str(item.get("name")) for item in result["hods"][:3])
        return output(context.institution_id, "faculty", result, f"Head of department for {result.get('department') or 'the institution'}: {names}.", rows_used=result["count"])

    async def find_staff(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.find_staff(context.principal, context.institution_id, department=args.get("department"), name_contains=args.get("name_contains"), limit=args.get("limit"))
        return output(context.institution_id, "staff", result, f"{result['count']} staff member(s) matched.", rows_used=result["count"])

    return (
        PlatformToolSpec(
            name="find_faculty", group="faculty", description="List faculty by department, designation, or name.",
            required_capability=Capability.FACULTY_READ, handler=find_faculty, risk=RiskLevel.READ, returns="{count, faculty[]}",
            parameters=(param("department", "string", "Department", max_length=120), param("designation", "string", "Designation filter", max_length=80), param("name_contains", "string", "Partial name", max_length=80), param("limit", "integer", "Maximum rows", minimum=1, maximum=500, default=100), param("fields", "array", "Fields to return", max_length=20)),
        ),
        PlatformToolSpec(
            name="find_hod", group="faculty", description="Find the head of department for a department or program (used to address reports and emails).",
            required_capability=Capability.FACULTY_READ, handler=find_hod, risk=RiskLevel.READ, returns="{department, count, hods[]}",
            parameters=(param("department", "string", "Department", max_length=120), param("program", "string", "Program code, when the department is unknown", max_length=60)),
            examples=("Who is the HOD of MBA?",),
        ),
        PlatformToolSpec(
            name="find_staff", group="faculty", description="List non-teaching staff by department or name.",
            required_capability=Capability.FACULTY_READ, handler=find_staff, risk=RiskLevel.READ, returns="{count, staff[]}",
            parameters=(param("department", "string", "Department or office", max_length=120), param("name_contains", "string", "Partial name", max_length=80), param("limit", "integer", "Maximum rows", minimum=1, maximum=500, default=100)),
        ),
    )


__all__ = ["build_faculty_tools"]
