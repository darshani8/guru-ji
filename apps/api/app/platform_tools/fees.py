"""Fee tools."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import describe_filters, output
from .context import PlatformServices


def inr(amount: float) -> str:
    """Rupees with Indian digit grouping plus a spoken-scale hint: ₹1,69,15,300 (₹1.69 crore)."""
    rupees = int(round(abs(amount)))
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    text = "₹" + ",".join(([head] if head else []) + groups + [tail])
    if rupees >= 10_000_000:
        text += f" ({rupees / 10_000_000:.2f} crore)"
    elif rupees >= 100_000:
        text += f" ({rupees / 100_000:.2f} lakh)"
    return ("-" if amount < 0 else "") + text


def build_fee_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    data = services.data
    common = (param("program", "string", "Program code", max_length=60), param("semester", "string", "Semester", max_length=20), param("academic_year", "string", "Academic year label", max_length=20))

    async def get_pending_fees(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.pending_fees(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), academic_year=args.get("academic_year"), limit=args.get("limit"), include_students=args.get("include_students", True))
        summary = f"{result['count']} student(s){describe_filters(result['filters'])} have pending fees totalling {inr(result['total_outstanding'])}."
        if result["students_evaluated"] and not result["dues_recorded"]:
            summary = f"The fee records{describe_filters(result['filters'])} list payments only ({result['students_evaluated']} student(s)); no amounts due are recorded, so pending dues cannot be worked out. Import a fee file with the amount due to track dues."
        warnings = [] if result["students_evaluated"] else [{"code": "no_fee_data", "message": "No fee records match the filters; import fee data first."}]
        return output(context.institution_id, "fees", result, summary, rows_used=result["students_evaluated"], records_returned=result.get("returned", 0), warnings=warnings)

    async def get_fee_summary(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
        result = data.fee_summary(context.principal, context.institution_id, program=args.get("program"), semester=args.get("semester"), academic_year=args.get("academic_year"))
        filters = result["filters"]
        if not result["students_evaluated"]:
            summary = f"No fee records found{describe_filters(filters)}."
        elif result["total_due"]:
            summary = f"Fees{describe_filters(filters)}: due {inr(result['total_due'])}, collected {inr(result['total_paid'])}, outstanding {inr(result['total_outstanding'])} across {result['students_with_dues']} student(s) with dues."
        else:
            # Payment-only records (an admission fee register) carry no amount due, so dues cannot be judged.
            summary = f"Fees collected{describe_filters(filters)}: {inr(result['total_paid'])} from {result['students_evaluated']} student(s). No amounts due are recorded, so pending dues are not known."
        if result["students_evaluated"] and not filters.get("academic_year") and len(result["by_academic_year"]) > 1:
            summary += " By academic year: " + "; ".join(f"{item['academic_year']} {inr(item['amount_paid'])}" for item in result["by_academic_year"]) + "."
        if not filters.get("program") and len(result["by_program"]) > 1:
            summary += " By program: " + "; ".join(f"{item['program']} {inr(item['amount_paid'])}" for item in result["by_program"]) + "."
        warnings = [] if result["students_evaluated"] else [{"code": "no_fee_data", "message": "No fee records match the filters; import fee data first."}]
        return output(context.institution_id, "fees", result, summary, rows_used=result["students_evaluated"], records_returned=0, warnings=warnings)

    return (
        PlatformToolSpec(
            name="get_pending_fees", group="fees", description="Students with outstanding fee balances, with the total outstanding amount.",
            required_capability=Capability.FEES_READ, handler=get_pending_fees, risk=RiskLevel.READ, returns="{count, total_outstanding, students[]}",
            parameters=(*common, param("limit", "integer", "Maximum students to list", minimum=1, maximum=500, default=100), param("include_students", "boolean", "Return the list, not only totals", default=True)),
            examples=("How many students have pending fees?", "List MBA students with fee dues"),
        ),
        PlatformToolSpec(
            name="get_fee_summary", group="fees", description="Fee collection summary: total due, collected, outstanding, collection percentage, with totals by program and by academic year. Filter by program (BCA, BCOM, BBA, MBA) and academic_year (for example 2024-25).",
            required_capability=Capability.FEES_READ, handler=get_fee_summary, risk=RiskLevel.READ, returns="{total_due, total_paid, total_outstanding, collection_percent, by_program[], by_academic_year[]}", parameters=common,
            examples=("How much fee was collected in 2024-25?", "Program-wise fee collection", "BCA fees collected in 2025-26"),
        ),
    )


__all__ = ["build_fee_tools"]
