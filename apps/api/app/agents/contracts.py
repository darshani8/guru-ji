"""Typed contracts between the master agent, planners, and channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.principals import InstitutionScope, Principal

MAX_PLAN_STEPS = 8
MAX_COMMAND_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class AgentCommand:
    request_id: str
    principal: Principal
    scope: InstitutionScope
    text: str
    channel: str = "text"
    conversation_id: str | None = None
    approval_id: str | None = None
    run_in_background: bool = False

    def __post_init__(self) -> None:
        text = " ".join(self.text.split())
        if not text:
            raise ValueError("command text must not be blank")
        if len(text) > MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {MAX_COMMAND_CHARS} characters")
        object.__setattr__(self, "text", text)
        if not self.request_id.strip():
            raise ValueError("request_id must not be blank")


@dataclass(slots=True)
class PlanStep:
    step_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    purpose: str = ""
    depends_on: tuple[str, ...] = ()
    bindings: dict[str, str] = field(default_factory=dict)  # argument -> "$step_id.path[*].field"
    agent: str = "data"

    def as_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "tool": self.tool, "arguments": dict(self.arguments), "purpose": self.purpose, "depends_on": list(self.depends_on), "bindings": dict(self.bindings), "agent": self.agent}


@dataclass(slots=True)
class AgentPlan:
    intent: str
    steps: list[PlanStep] = field(default_factory=list)
    clarification: str | None = None
    summary: str = ""
    planner: str = "deterministic"
    confidence: float = 1.0
    entities: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.steps) > MAX_PLAN_STEPS:
            raise ValueError(f"a plan cannot contain more than {MAX_PLAN_STEPS} steps")
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("plan step identifiers must be unique")
        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in ids:
                    raise ValueError(f"step {step.step_id} depends on unknown step {dependency}")

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "summary": self.summary, "planner": self.planner, "confidence": round(self.confidence, 3), "clarification": self.clarification, "steps": [step.as_dict() for step in self.steps], "entities": dict(self.entities)}


@dataclass(slots=True)
class StepResult:
    step_id: str
    tool: str
    status: str
    summary: str = ""
    data: Any = None
    records_returned: int = 0
    duration_ms: int = 0
    warnings: list[dict[str, str]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    denial_reason: str | None = None
    approval: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def as_dict(self, *, include_data: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id, "tool": self.tool, "status": self.status, "summary": self.summary, "records_returned": self.records_returned,
            "duration_ms": self.duration_ms, "warnings": list(self.warnings), "artifacts": list(self.artifacts), "denial_reason": self.denial_reason, "approval": self.approval,
        }
        if include_data:
            payload["data"] = self.data
        return payload


@dataclass(slots=True)
class AgentResponse:
    request_id: str
    status: str  # complete | partial | refused | needs_input | approval_required | failed | accepted
    answer: str
    intent: str = "unknown"
    plan: dict[str, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    clarification: str | None = None
    refusal_reason: str | None = None
    generation_mode: str = "deterministic"
    duration_ms: int = 0
    job_id: str | None = None
    conversation_id: str | None = None

    def as_dict(self, *, include_data: bool = True) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "status": self.status, "answer": self.answer, "intent": self.intent, "plan": dict(self.plan),
            "steps": [step.as_dict(include_data=include_data) for step in self.steps], "sources": list(self.sources), "citations": list(self.sources),
            "warnings": list(self.warnings), "artifacts": list(self.artifacts), "approval": self.approval, "clarification": self.clarification,
            "refusal_reason": self.refusal_reason, "generation_mode": self.generation_mode, "duration_ms": self.duration_ms, "job_id": self.job_id,
            "conversation_id": self.conversation_id, "tool_names": [step.tool for step in self.steps],
        }


__all__ = ["MAX_COMMAND_CHARS", "MAX_PLAN_STEPS", "AgentCommand", "AgentPlan", "AgentResponse", "PlanStep", "StepResult"]
