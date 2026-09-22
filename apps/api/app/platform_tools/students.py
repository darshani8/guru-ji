"""Student tools: counts, bounded lists, one record."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import describe_filters, output
from .context import PlatformServices

_PROGRAM = param("program", "string", "Program or degree code such as MBA, BCA, BE", max_length=60)
_SEMESTER = param("semester", "string", "Semester number (1-20); words like 'third' are accepted", max_length=20)
_DEPARTMENT = param("department", "string", "Department name or code", max_length=120)


def build_student_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data

    async def count_students(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.count_students(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), department=args.get("department"), status=args.get("status"))
        return output(context.institution_id, "students", result, f"{result['count']} student(s){describe_filters(result['filters'])}.", rows_used=result["count"], records_returned=0)

    async def find_students(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.find_students(
            context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), department=args.get("department"),
            section=args.get("section"), name_contains=args.get("name_contains"), status=args.get("status"), limit=args.get("limit"), fields=args.get("fields"),
        )
        summary = f"{result['count']} student(s) match{describe_filters(result['filters'])}; returning {result['returned']}."
        return output(context.institution_id, "students", result, summary, rows_used=result["returned"], complete=result["returned"] >= result["count"])

    async def get_student(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        record = data.get_student(context.principal, context.institution_id, args["student_id"], fields=args.get("fields"))
        if record is None:
            return output(context.institution_id, "students", {"found": False, "student_id": args["student_id"]}, f"No student with ID {args['student_id']} was found.", rows_used=0)
        return output(context.institution_id, "students", {"found": True, "student": record}, f"Found student {record.get('name')} ({record.get('student_id')}).", rows_used=1)

    return (
        PlatformToolSpec(
            name="count_students", group="students", description="Count students, optionally filtered by program, semester, department, or status. Use for 'how many' questions.",
            required_capability=Capability.STUDENTS_READ, handler=count_students, risk=RiskLevel.READ, returns="{count, filters}",
            parameters=(_PROGRAM, _SEMESTER, _DEPARTMENT, param("status", "string", "Enrollment status filter", max_length=40)),
            examples=("How many MBA students are there?", "Count semester 3 BCA students"),
        ),
        PlatformToolSpec(
            name="find_students", group="students", description="List students matching filters with minimal fields (ID, name, program, semester). Contact fields require extra permission.",
            required_capability=Capability.STUDENTS_READ, handler=find_students, risk=RiskLevel.READ, returns="{count, returned, students[]}",
            parameters=(_PROGRAM, _SEMESTER, _DEPARTMENT, param("section", "string", "Section", max_length=20), param("name_contains", "string", "Partial name match", max_length=80), param("status", "string", "Enrollment status", max_length=40), param("limit", "integer", "Maximum rows (1-500)", minimum=1, maximum=500, default=100), param("fields", "array", "Fields to return", max_length=30)),
            examples=("Give me the names of MBA semester 1 students",),
        ),
        PlatformToolSpec(
            name="get_student", group="students", description="Fetch one student by institution ID (USN / roll number).",
            required_capability=Capability.STUDENTS_READ, handler=get_student, risk=RiskLevel.READ, returns="{found, student}",
            parameters=(param("student_id", "string", "Student identifier", required=True, max_length=40), param("fields", "array", "Fields to return", max_length=30)),
        ),
    )


__all__ = ["build_student_tools"]
