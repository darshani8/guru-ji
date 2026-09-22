"""Action tools: reports, email, notifications, record updates."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import output
from .context import PlatformServices


def build_action_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    async def generate_report(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        rows = [row for row in args.get("rows", []) if isinstance(row, dict)]
        columns = list(args.get("columns") or [])
        if not columns and rows:
            columns = list(dict.fromkeys(key for row in rows for key in row))
        result = services.reports.generate(
            context.principal, context.institution_id, title=args["title"], columns=columns, rows=rows, format_name=args.get("format", "xlsx"),
            tool_name="generate_report", subtitle=args.get("subtitle"),
        )
        artifact = {"type": "report", **result}
        summary = f"Generated {result['format'].upper()} report '{result['title']}' with {result['row_count']} row(s)."
        return output(context.institution_id, "reports", result, summary, rows_used=result["row_count"], records_returned=0, artifacts=[artifact])

    async def list_reports(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        rows = services.reports.list(context.principal, context.institution_id, limit=args.get("limit", 20))
        return output(context.institution_id, "reports", {"count": len(rows), "reports": rows}, f"{len(rows)} generated report(s) available.", rows_used=len(rows))

    async def send_email(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = services.email.send(context.principal, context.institution_id, recipients=args["recipients"], subject=args["subject"], body=args["body"], report_ids=args.get("report_ids", []))
        to = ", ".join(item["email"] for item in result["recipients"])
        summary = f"Email '{args['subject']}' {result['status']} via {result['provider']} to {to}."
        warnings = [{"code": "unresolved_recipients", "message": "Not sent to: " + ", ".join(result["unresolved_recipients"])}] if result["unresolved_recipients"] else []
        if result.get("error"):
            warnings.append({"code": "email_delivery_error", "message": str(result["error"])})
        return output(context.institution_id, "email", result, summary, rows_used=len(result["recipients"]), records_returned=0, warnings=warnings, artifacts=[{"type": "email", "email_id": result["email_id"], "status": result["status"]}])

    async def create_notification(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = services.notifications.create(context.principal, context.institution_id, recipient_ids=args.get("recipient_ids", []), title=args["title"], body=args["body"], reference_type=args.get("reference_type"), reference_id=args.get("reference_id"))
        return output(context.institution_id, "notifications", result, f"Created {result['count']} notification(s) titled '{args['title']}'.", rows_used=result["count"], records_returned=0)

    async def update_student_record(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = services.data.update_student(context.principal, context.institution_id, args["student_id"], args["changes"], locator=f"agent:{context.request_id}")
        return output(context.institution_id, "students", result, f"Updated {', '.join(result['changed_fields'])} for student {result['student_id']}.", rows_used=1, records_returned=0)

    return (
        PlatformToolSpec(
            name="generate_report", group="reports", description="Create a downloadable CSV, Excel (xlsx), or PDF report from rows produced by an earlier data tool.",
            required_capability=Capability.REPORTS_GENERATE, handler=generate_report, risk=RiskLevel.WRITE, returns="{report_id, download_path, row_count}",
            parameters=(param("title", "string", "Report title", required=True, max_length=150), param("format", "string", "Output format", enum=("csv", "xlsx", "pdf"), default="xlsx"), param("columns", "array", "Column order", max_length=60), param("rows", "array", "Row objects (from a previous tool result)", items_type="object", max_length=50_000), param("subtitle", "string", "Optional subtitle", max_length=200)),
            examples=("Create an Excel report of students below 75% attendance",),
        ),
        PlatformToolSpec(name="list_reports", group="reports", description="List recently generated reports and their download paths.", required_capability=Capability.REPORTS_GENERATE, handler=list_reports, returns="{count, reports[]}", parameters=(param("limit", "integer", "Maximum rows", minimum=1, maximum=100, default=20),)),
        PlatformToolSpec(
            name="send_email", group="email", description="Email institution members (faculty/staff IDs or names from the directory, or addresses in an allowed domain), optionally attaching generated reports.",
            required_capability=Capability.ACTIONS_EMAIL, handler=send_email, risk=RiskLevel.WRITE, returns="{email_id, status, recipients[]}",
            parameters=(param("recipients", "array", "Faculty/staff IDs, directory names, or allowed email addresses", required=True, max_length=25), param("subject", "string", "Subject", required=True, max_length=200), param("body", "string", "Message body", required=True, max_length=8000), param("report_ids", "array", "Report IDs to attach", max_length=5)),
            examples=("Send the report to the HOD",),
        ),
        PlatformToolSpec(
            name="create_notification", group="notifications", description="Create in-app notifications for named users (defaults to the requester).",
            required_capability=Capability.ACTIONS_NOTIFY, handler=create_notification, risk=RiskLevel.WRITE, returns="{notification_ids[]}",
            parameters=(param("title", "string", "Title", required=True, max_length=200), param("body", "string", "Body", required=True, max_length=4000), param("recipient_ids", "array", "User identifiers", max_length=200), param("reference_type", "string", "Linked object type", max_length=40), param("reference_id", "string", "Linked object ID", max_length=80)),
        ),
        PlatformToolSpec(
            name="update_student_record", group="records", description="Change permitted fields of one student record (semester, section, status, contact details). Requires explicit confirmation before it runs.",
            required_capability=Capability.RECORDS_WRITE, handler=update_student_record, risk=RiskLevel.HIGH_RISK, returns="{student_id, changed_fields, before, after}",
            parameters=(param("student_id", "string", "Student identifier", required=True, max_length=40), param("changes", "object", "Field -> new value", required=True, max_length=10)),
            examples=("Update the phone number of student MBA001 to 9876543210",),
        ),
    )


__all__ = ["build_action_tools"]
