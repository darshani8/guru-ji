"""End-to-end local read-only assistant service."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from ..config.source_registry import SourceRegistry
from ..connectors.registry import ConnectorRegistry
from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.requests import ChatRequest
from ..domain.results import ResultStatus
from ..persistence.database import InMemoryControlStore
from ..policy.query_limits import QueryLimits
from ..tools.registry import ToolRegistry
from ..providers.model_base import TextModel
from .answer_synthesizer import AssistantAnswer, apply_model_wording, synthesize
from .plan_validator import build_tool_plan
from .result_aggregator import aggregate
from .tool_executor import execute_plan


def _audit_outcome(answer: AssistantAnswer, results: tuple) -> AuditOutcome:
    if any(result.status is ResultStatus.UNAUTHORIZED for result in results):
        return AuditOutcome.DENIED
    if answer.status == "complete":
        return AuditOutcome.SUCCESS
    if answer.status == "partial":
        return AuditOutcome.PARTIAL
    return AuditOutcome.FAILED


@dataclass(slots=True)
class AssistantService:
    sources: SourceRegistry
    tools: ToolRegistry
    connectors: ConnectorRegistry
    store: InMemoryControlStore
    limits: QueryLimits = QueryLimits()
    model: TextModel | None = None
    model_max_tokens: int = 800

    async def ask(self, request: ChatRequest, principal) -> AssistantAnswer:
        started = monotonic()
        try:
            plan = build_tool_plan(request, self.tools, self.sources)
            results = await execute_plan(
                plan, principal, self.tools, self.connectors, request.institution_scope,
                request.request_id, self.limits,
            )
            aggregate(results)
            answer = synthesize(request.request_id, results)
            answer = await apply_model_wording(
                answer,
                self.model,
                max_tokens=self.model_max_tokens,
            )
            self.store.append_audit(AuditEvent(
                event_id=f"audit-{request.request_id}", event_type="assistant.ask", request_id=request.request_id,
                principal_id=principal.principal_id if principal.authenticated else None,
                conversation_id=request.conversation_id, source_ids=tuple(step.source_id for step in plan),
                tool_names=tuple(step.name for step in plan), outcome=_audit_outcome(answer, results),
                duration_ms=int((monotonic() - started) * 1000),
            ))
            return answer
        except (KeyError, ValueError) as exc:
            answer = AssistantAnswer(
                request_id=request.request_id, status="refused",
                answer="The request could not be planned safely.", refusal_reason=str(exc),
            )
            self.store.append_audit(AuditEvent(
                event_id=f"audit-{request.request_id}", event_type="assistant.ask", request_id=request.request_id,
                principal_id=getattr(principal, "principal_id", None), outcome=AuditOutcome.DENIED,
                duration_ms=int((monotonic() - started) * 1000),
            ))
            return answer


__all__ = ["AssistantService"]
