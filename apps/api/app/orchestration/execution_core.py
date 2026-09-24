"""Plan-only orchestration boundary for safe read-only requests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain.principals import Capability, Principal
from ..domain.requests import ChatRequest
from ..policy.authorization import authorize


class PlanStepKind(StrEnum):
    """Semantic steps; this layer never accepts or executes arbitrary SQL."""

    CLASSIFY_REQUEST = "classify_request"
    QUERY_APPROVED_SOURCES = "query_approved_sources"
    SYNTHESIZE_CITED_ANSWER = "synthesize_cited_answer"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One bounded step in a future execution plan."""

    step_id: str
    kind: PlanStepKind
    description: str
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("step_id must not be blank")
        if not self.description.strip():
            raise ValueError("description must not be blank")
        source_ids = tuple(source_id.strip() for source_id in self.source_ids)
        if any(not source_id for source_id in source_ids):
            raise ValueError("source_ids must not contain blank values")
        object.__setattr__(self, "source_ids", source_ids)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """An immutable authorization result plus semantic steps, not execution itself."""

    request_id: str
    authorized: bool
    steps: tuple[PlanStep, ...] = ()
    denial_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be blank")

        steps = tuple(self.steps)
        if len(steps) > 5:
            raise ValueError("a plan cannot contain more than five steps")
        object.__setattr__(self, "steps", steps)

        if self.authorized and not steps:
            raise ValueError("authorized plans require at least one step")
        if not self.authorized and steps:
            raise ValueError("denied plans cannot contain executable steps")
        if self.authorized and self.denial_reason is not None:
            raise ValueError("authorized plans cannot contain a denial reason")
        if not self.authorized and not self.denial_reason:
            raise ValueError("denied plans require a denial reason")


def build_read_only_plan(request: ChatRequest, principal: Principal) -> ExecutionPlan:
    """Authorize and describe work without contacting a connector or model provider."""

    decision = authorize(
        principal=principal,
        required_capability=Capability.ASK_READ_ONLY,
        requested_scope=request.institution_scope,
    )
    if not decision.allowed:
        return ExecutionPlan(
            request_id=request.request_id,
            authorized=False,
            denial_reason=decision.reason.value if decision.reason else "denied",
        )

    return ExecutionPlan(
        request_id=request.request_id,
        authorized=True,
        steps=(
            PlanStep(
                step_id="classify",
                kind=PlanStepKind.CLASSIFY_REQUEST,
                description="Classify the request against approved Agentic Saffron capabilities.",
            ),
            PlanStep(
                step_id="retrieve",
                kind=PlanStepKind.QUERY_APPROVED_SOURCES,
                description="Read only from sources allowed by the source registry and scope.",
                source_ids=request.source_ids,
            ),
            PlanStep(
                step_id="synthesize",
                kind=PlanStepKind.SYNTHESIZE_CITED_ANSWER,
                description="Synthesize a bounded answer with provenance and citations.",
            ),
        ),
    )


__all__ = ["ExecutionPlan", "PlanStep", "PlanStepKind", "build_read_only_plan"]
