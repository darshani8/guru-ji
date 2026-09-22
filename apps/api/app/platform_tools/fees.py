"""Fee tools."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import describe_filters, output
from .context import PlatformServices


def build_fee_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data
    common = (param("program", "string", "Program code", max_length=60), param("semester", "string", "Semester", max_length=20), param("academic_year", "string", "Academic year label", max_length=20))

    async def get_pending_fees(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.pending_fees(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), academic_year=args.get("academic_year"), limit=args.get("limit"), include_students=args.get("include_students", True))
        summary = f"{result['count']} student(s){describe_filters(result['filters'])} have pending fees totalling {result['total_outstanding']:,.2f}."
        warnings = [] if result["students_evaluated"] else [{"code": "no_fee_data", "message": "No fee records match the filters; import fee data first."}]
        return output(context.institution_id, "fees", result, summary, rows_used=result["students_evaluated"], records_returned=result.get("returned", 0), warnings=warnings)

    async def get_fee_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.fee_summary(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), academic_year=args.get("academic_year"))
        summary = f"Fees{describe_filters(result['filters'])}: due {result['total_due']:,.2f}, collected {result['total_paid']:,.2f}, outstanding {result['total_outstanding']:,.2f} across {result['students_with_dues']} student(s) with dues."
        return output(context.institution_id, "fees", result, summary, rows_used=result["students_evaluated"], records_returned=0)

    return (
        PlatformToolSpec(
            name="get_pending_fees", group="fees", description="Students with outstanding fee balances, with the total outstanding amount.",
            required_capability=Capability.FEES_READ, handler=get_pending_fees, risk=RiskLevel.READ, returns="{count, total_outstanding, students[]}",
            parameters=(*common, param("limit", "integer", "Maximum students to list", minimum=1, maximum=500, default=100), param("include_students", "boolean", "Return the list, not only totals", default=True)),
            examples=("How many students have pending fees?", "List MBA students with fee dues"),
        ),
        PlatformToolSpec(
            name="get_fee_summary", group="fees", description="Fee collection summary: total due, paid, outstanding, collection percentage.",
            required_capability=Capability.FEES_READ, handler=get_fee_summary, risk=RiskLevel.READ, returns="{total_due, total_paid, total_outstanding, collection_percent}", parameters=common,
        ),
    )


__all__ = ["build_fee_tools"]
