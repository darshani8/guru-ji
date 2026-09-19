"""Deterministic answer synthesis for the local reference implementation."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.provenance import Provenance, Warning
from ..domain.results import ResultStatus, ToolResult


@dataclass(frozen=True, slots=True)
class AssistantAnswer:
    request_id: str
    status: str
    answer: str
    citations: tuple[dict[str, str], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()
    tool_names: tuple[str, ...] = ()
    refusal_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id, "status": self.status, "answer": self.answer,
            "citations": list(self.citations), "warnings": list(self.warnings),
            "tool_names": list(self.tool_names), "refusal_reason": self.refusal_reason,
        }


def _citation(provenance: Provenance) -> dict[str, str]:
    return {
        "source_id": provenance.source_id,
        "title": f"{provenance.source_id} ({provenance.source_type.value})",
        "locator": provenance.data_period.started_at.date().isoformat() if provenance.data_period else "retrieved",
    }


def synthesize(request_id: str, results: tuple[ToolResult, ...]) -> AssistantAnswer:
    successful = [item for item in results if item.status is ResultStatus.SUCCESS]
    if not successful:
        return AssistantAnswer(
            request_id=request_id, status="refused", answer="I could not provide an answer from an approved source.",
            warnings=tuple({"code": "no_usable_source", "message": "No approved source returned a usable result."} for _ in [0]),
            tool_names=tuple(item.tool_name for item in results), refusal_reason="No approved source returned a usable result.",
        )
    fragments: list[str] = []
    for item in successful:
        if item.tool_name == "institution.overview" and isinstance(item.data, dict):
            fragments.append(
                f"The demo institution reports {item.data.get('active_students', 'an unknown number')} active students, "
                f"{item.data.get('active_faculty', 'an unknown number')} active faculty, and "
                f"an aggregate attendance rate of {item.data.get('attendance_rate_percent', 'unknown')}%."
            )
        elif item.tool_name == "institution.attendance_summary" and isinstance(item.data, dict):
            fragments.append(
                f"The current demo-term aggregate attendance rate is {item.data.get('attendance_rate_percent', 'unknown')}%, "
                f"with {item.data.get('at_risk_cohort_count', 'unknown')} at-risk cohorts flagged."
            )
        elif item.tool_name == "institution.source_health" and isinstance(item.data, dict):
            fragments.append(f"The approved demo source is {item.data.get('status', 'unknown')} and {item.data.get('freshness', 'unknown')}.")
    provenance = tuple(prov for item in successful for prov in item.provenance)
    warnings = tuple(warning for item in results for warning in item.warnings)
    status = "partial" if any(item.status is not ResultStatus.SUCCESS for item in results) else "complete"
    return AssistantAnswer(
        request_id=request_id, status=status, answer=" ".join(fragments),
        citations=tuple(_citation(item) for item in provenance),
        warnings=tuple({"code": item.code, "message": item.message, **({"source_id": item.source_id} if item.source_id else {})} for item in warnings),
        tool_names=tuple(item.tool_name for item in results),
    )


__all__ = ["AssistantAnswer", "synthesize"]
