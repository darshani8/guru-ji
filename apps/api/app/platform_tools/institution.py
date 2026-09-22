"""Institution-wide overview, programs, departments, courses, events, admissions, exams."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import describe_filters, output
from .context import PlatformServices


def build_institution_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data

    async def get_institution_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.institution_summary(context.principal, context.institution_id)
        counts = result["record_counts"]
        summary = f"{result.get('institution_name') or context.institution_id}: {counts.get('student', 0)} students, {counts.get('faculty', 0)} faculty, {len(result.get('programs', []))} programs on record."
        if "attendance" in result and result["attendance"].get("average_percent") is not None:
            summary += f" Average attendance {result['attendance']['average_percent']}%."
        if "fees" in result:
            summary += f" Outstanding fees {result['fees']['total_outstanding']:,.2f} across {result['fees']['students_with_dues']} student(s)."
        return output(context.institution_id, "institution", result, summary, rows_used=sum(counts.values()), records_returned=0)

    async def list_programs(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.list_programs(context.principal, context.institution_id)
        codes = ", ".join(str(item["code"]) for item in result["programs"][:20])
        return output(context.institution_id, "programs", result, f"{result['count']} program(s): {codes}.", rows_used=result["count"])

    async def list_departments(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.list_departments(context.principal, context.institution_id)
        names = ", ".join(str(item.get("name") or item.get("code")) for item in result["departments"][:20])
        return output(context.institution_id, "departments", result, f"{result['count']} department(s): {names}.", rows_used=result["count"])

    async def list_courses(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.list_courses(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), limit=args.get("limit"))
        return output(context.institution_id, "courses", result, f"{result['count']} course(s) matched.", rows_used=result["count"])

    async def list_upcoming_events(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.upcoming_events(context.principal, context.institution_id, days=args.get("days", 30), limit=args.get("limit"))
        titles = "; ".join(f"{item['title']} ({item['event_date']})" for item in result["events"][:5])
        return output(context.institution_id, "events", result, f"{result['count']} event(s) between {result['from']} and {result['to']}." + (f" Next: {titles}." if titles else ""), rows_used=result["count"])

    async def get_admissions_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.admissions_summary(context.principal, context.institution_id, program=args.get("program"), academic_year=args.get("academic_year"))
        statuses = ", ".join(f"{status}: {count}" for status, count in sorted(result["by_status"].items()))
        return output(context.institution_id, "admissions", result, f"{result['applications']} application(s){describe_filters(result['filters'])}" + (f" ({statuses})." if statuses else "."), rows_used=result["applications"], records_returned=0)

    async def get_exam_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.exam_summary(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), course_code=args.get("course_code"), exam_name=args.get("exam_name"))
        if not result["results"]:
            return output(context.institution_id, "exams", result, "No exam results match the filters.", rows_used=0, warnings=[{"code": "no_exam_data", "message": "Import exam results first."}])
        rate = f"{result['pass_rate_percent']}%" if result["pass_rate_percent"] is not None else "unknown"
        return output(context.institution_id, "exams", result, f"{result['results']} result(s){describe_filters(result['filters'])}: {result['passed']} passed, {result['failed']} failed, pass rate {rate}.", rows_used=result["results"], records_returned=0)

    async def get_student_results(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.student_results(context.principal, context.institution_id, args["student_id"], limit=args.get("limit"))
        return output(context.institution_id, "exams", result, f"{result['count']} result(s) for student {args['student_id']}.", rows_used=result["count"])

    return (
        PlatformToolSpec(name="get_institution_summary", group="institution", description="Headline counts and indicators for the whole institution (students, faculty, programs, attendance, fees, admissions).", required_capability=Capability.ASK_READ_ONLY, handler=get_institution_summary, returns="{record_counts, programs, attendance, fees}", examples=("Give me an overview of our institution",)),
        PlatformToolSpec(name="list_programs", group="institution", description="List programs offered.", required_capability=Capability.ASK_READ_ONLY, handler=list_programs, returns="{count, programs[]}"),
        PlatformToolSpec(name="list_departments", group="institution", description="List departments and their heads.", required_capability=Capability.ASK_READ_ONLY, handler=list_departments, returns="{count, departments[]}"),
        PlatformToolSpec(name="list_courses", group="institution", description="List courses/subjects for a program and semester.", required_capability=Capability.ASK_READ_ONLY, handler=list_courses, returns="{count, courses[]}", parameters=(param("program", "string", "Program code", max_length=60), param("semester", "string", "Semester", max_length=20), param("limit", "integer", "Maximum rows", minimum=1, maximum=500, default=100))),
        PlatformToolSpec(name="list_upcoming_events", group="institution", description="Institutional events in the next N days.", required_capability=Capability.ASK_READ_ONLY, handler=list_upcoming_events, returns="{count, events[]}", parameters=(param("days", "integer", "Look-ahead window in days", minimum=1, maximum=365, default=30), param("limit", "integer", "Maximum rows", minimum=1, maximum=500, default=50))),
        PlatformToolSpec(name="get_admissions_summary", group="institution", description="Admission applications by status and program.", required_capability=Capability.ASK_READ_ONLY, handler=get_admissions_summary, returns="{applications, by_status, by_program}", parameters=(param("program", "string", "Program code", max_length=60), param("academic_year", "string", "Academic year", max_length=20))),
        PlatformToolSpec(name="get_exam_summary", group="exams", description="Exam performance: pass rate and per-course averages.", required_capability=Capability.EXAMS_READ, handler=get_exam_summary, risk=RiskLevel.READ, returns="{results, passed, failed, pass_rate_percent, courses[]}", parameters=(param("program", "string", "Program code", max_length=60), param("semester", "string", "Semester", max_length=20), param("course_code", "string", "Course/subject code", max_length=40), param("exam_name", "string", "Exam name", max_length=80))),
        PlatformToolSpec(name="get_student_results", group="exams", description="Exam results for one student.", required_capability=Capability.EXAMS_READ, handler=get_student_results, returns="{count, results[]}", parameters=(param("student_id", "string", "Student identifier", required=True, max_length=40), param("limit", "integer", "Maximum rows", minimum=1, maximum=500, default=100))),
    )


__all__ = ["build_institution_tools"]
