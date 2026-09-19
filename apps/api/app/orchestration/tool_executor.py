"""Policy-aware sequential tool execution."""

from __future__ import annotations

from ..connectors.base import ConnectorContext
from ..connectors.registry import ConnectorRegistry
from ..domain.principals import InstitutionScope, Principal
from ..domain.results import ResultStatus, ToolResult
from ..policy.pdp import LocalPolicyDecisionPoint, PolicyDecision, PolicyDecisionPoint
from ..policy.query_limits import QueryLimits
from ..tools.registry import ToolRegistry
from .plan_validator import PlannedTool


async def execute_plan(
    plan: tuple[PlannedTool, ...], principal: Principal, tools: ToolRegistry,
    connectors: ConnectorRegistry, requested_scope: InstitutionScope, request_id: str,
    limits: QueryLimits, pdp: PolicyDecisionPoint | None = None,
    decision_log: list[PolicyDecision] | None = None,
) -> tuple[ToolResult, ...]:
    decision_point = pdp or LocalPolicyDecisionPoint()
    results: list[ToolResult] = []
    for step in plan:
        tool = tools.get(step.name)
        decision = decision_point.evaluate(
            principal=principal,
            required_capability=tool.required_capability,
            action="retrieve",
            resource_type="connector_resource",
            resource_id=step.source_id,
            requested_scope=requested_scope,
        )
        if decision_log is not None:
            decision_log.append(decision)
        if not decision.allowed:
            results.append(ToolResult(tool_name=step.name, status=ResultStatus.UNAUTHORIZED))
            continue
        connector = connectors.get(step.source_id)
        results.append(await connector.execute(
            step.name, step.arguments,
            ConnectorContext(
                request_id=request_id,
                source_id=step.source_id,
                limits=limits,
                principal_id=principal.principal_id,
                principal_type=principal.principal_type,
                institution_scope=requested_scope,
            ),
        ))
    return tuple(results)


__all__ = ["execute_plan"]
