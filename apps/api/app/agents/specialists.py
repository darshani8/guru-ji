"""Specialised agents. Each owns a set of tool groups and runs steps through the gateway."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

from ..gateway.gateway import ToolGateway
from ..gateway.spec import ToolCallContext
from .contracts import PlanStep, StepResult


@dataclass(slots=True)
class SpecializedAgent:
    name: str
    groups: frozenset[str]
    gateway: ToolGateway

    def owns(self, group: str) -> bool:
        return group in self.groups

    async def run(self, step: PlanStep, arguments: dict[str, Any], context: ToolCallContext) -> StepResult:
        started = monotonic()
        invocation = await self.gateway.invoke(step.tool, arguments, context)
        return StepResult(
            step_id=step.step_id, tool=step.tool, status=invocation.status, summary=invocation.summary or (invocation.denial_reason or ""), data=invocation.data,
            records_returned=invocation.records_returned, duration_ms=int((monotonic() - started) * 1000), warnings=list(invocation.warnings),
            provenance=list(invocation.provenance), artifacts=list(invocation.artifacts), denial_reason=invocation.denial_reason, approval=invocation.approval,
        )


def build_specialists(gateway: ToolGateway) -> dict[str, SpecializedAgent]:
    return {
        "data": SpecializedAgent("data", frozenset({"students", "attendance", "fees", "faculty", "institution", "exams", "documents", "ingestion"}), gateway),
        "action": SpecializedAgent("action", frozenset({"reports", "email", "notifications", "records"}), gateway),
        "internet": SpecializedAgent("internet", frozenset({"internet"}), gateway),
    }


__all__ = ["SpecializedAgent", "build_specialists"]
