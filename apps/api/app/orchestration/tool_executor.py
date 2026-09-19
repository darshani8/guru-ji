"""Policy-aware sequential tool execution."""

from __future__ import annotations

from ..connectors.base import ConnectorContext
from ..connectors.registry import ConnectorRegistry
from ..domain.principals import InstitutionScope, Principal
from ..domain.results import ResultStatus, ToolResult
from ..policy.query_limits import QueryLimits
from ..tools.registry import ToolRegistry
from .plan_validator import PlannedTool


def _has_capability(principal: Principal, capability: object) -> bool:
    return capability in principal.capabilities


def _has_scope(principal: Principal, requested_scope: InstitutionScope) -> bool:
    return any(scope.covers(requested_scope) for scope in principal.scopes)


async def execute_plan(
    plan: tuple[PlannedTool, ...], principal: Principal, tools: ToolRegistry,
    connectors: ConnectorRegistry, requested_scope: InstitutionScope, request_id: str,
    limits: QueryLimits,
) -> tuple[ToolResult, ...]:
    results: list[ToolResult] = []
    for step in plan:
        tool = tools.get(step.name)
        if not _has_capability(principal, tool.required_capability) or not _has_scope(principal, requested_scope):
            results.append(ToolResult(tool_name=step.name, status=ResultStatus.UNAUTHORIZED))
            continue
        connector = connectors.get(step.source_id)
        results.append(await connector.execute(
            step.name, step.arguments,
            ConnectorContext(request_id=request_id, source_id=step.source_id, limits=limits),
        ))
    return tuple(results)


__all__ = ["execute_plan"]
