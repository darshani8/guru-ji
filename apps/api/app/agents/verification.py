"""Post-execution checks so the reported result matches what actually happened."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import AgentPlan, StepResult


def verify(plan: AgentPlan, results: Sequence[StepResult]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    by_id = {result.step_id: result for result in results}
    for step in plan.steps:
        result = by_id.get(step.step_id)
        if result is None:
            warnings.append({"code": "step_not_executed", "message": f"Step {step.step_id} ({step.tool}) did not run because a dependency failed."})
            continue
        if not result.ok:
            continue
        data = result.data if isinstance(result.data, Mapping) else {}
        if step.tool in {"find_low_attendance", "get_pending_fees", "find_students"}:
            count = data.get("count")
            listed = data.get("students")
            if isinstance(count, int) and isinstance(listed, list) and len(listed) < count:
                warnings.append({"code": "list_truncated", "message": f"{step.tool} returned {len(listed)} of {count} matching students."})
        if step.tool == "generate_report":
            rows = data.get("row_count")
            if rows == 0:
                warnings.append({"code": "empty_report", "message": "The generated report has no rows."})
        if step.tool == "send_email":
            if not data.get("recipients"):
                warnings.append({"code": "email_no_recipients", "message": "The email had no resolved recipients."})
            if data.get("status") == "failed":
                warnings.append({"code": "email_failed", "message": "Email delivery failed; see the outbox for details."})
        if step.tool == "internet_investigate":
            findings = data.get("findings") if isinstance(data.get("findings"), list) else []
            if any(not isinstance(item, Mapping) or not item.get("url") for item in findings):
                warnings.append({"code": "finding_without_source", "message": "An internet finding lacked a source URL and should not be trusted."})
        if step.tool == "search_documents" and not data.get("sources"):
            warnings.append({"code": "no_document_sources", "message": "No document passage supported the answer."})
    return warnings


def overall_status(plan: AgentPlan, results: Sequence[StepResult]) -> str:
    if not results:
        return "failed"
    if any(result.status == "approval_required" for result in results):
        return "approval_required"
    statuses = [result.status for result in results]
    if all(status == "success" for status in statuses) and len(results) == len(plan.steps):
        return "complete"
    if all(status in {"denied", "unknown_tool"} for status in statuses):
        return "refused"
    if any(status == "success" for status in statuses):
        return "partial"
    return "failed"


def _summarise(value: Any) -> str:
    return str(value)


__all__ = ["overall_status", "verify"]
