"""Deterministic request classification and plan validation."""

from __future__ import annotations

from dataclasses import dataclass

from ..config.source_registry import SourceLifecycleStatus, SourceRegistry
from ..domain.requests import ChatRequest
from ..tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class PlannedTool:
    name: str
    source_id: str
    arguments: dict[str, object]


PUBLIC_WEB_INTENT_MARKERS = (
    "search the web",
    "web search",
    "search online",
    "look online",
    "internet search",
    "public web",
    "online sources",
    "official websites",
    "official website",
)


def is_public_web_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.lower().split())
    return any(marker in normalized for marker in PUBLIC_WEB_INTENT_MARKERS)


def classify_prompt(prompt: str) -> tuple[str, ...]:
    normalized = prompt.lower()
    if any(word in normalized for word in ("health", "availability", "source")):
        return ("institution.source_health",)
    if any(word in normalized for word in ("attendance", "present", "absent", "at risk")):
        return ("institution.attendance_summary",)
    return ("institution.overview",)


def build_tool_plan(request: ChatRequest, tools: ToolRegistry, sources: SourceRegistry) -> tuple[PlannedTool, ...]:
    tool_names = classify_prompt(request.prompt)
    candidate_ids = request.source_ids or tuple(
        definition.source_id for definition in sources.for_institution(request.institution_scope.college_id)
    )
    if not candidate_ids:
        raise ValueError(f"no approved source is registered for institution: {request.institution_scope.college_id}")

    planned: list[PlannedTool] = []
    for source_id in candidate_ids:
        definition = sources.get(source_id)
        if definition.status is not SourceLifecycleStatus.ACTIVE:
            raise ValueError(f"source is not active: {source_id}")
        if definition.institution_id != request.institution_scope.college_id:
            raise ValueError(f"source is outside the requested institution scope: {source_id}")
        for tool_name in tool_names:
            tool = tools.get(tool_name)
            if request.institution_scope.department_id and not tool.supports_department_scope:
                raise ValueError(f"tool does not support Department scope: {tool_name}")
            if request.institution_scope.batch_id and not tool.supports_batch_scope:
                raise ValueError(f"tool does not support Batch scope: {tool_name}")
            if not tool.allows_source(source_id):
                raise ValueError(f"tool is not approved for source: {tool_name}")
            if tool_name not in definition.allowed_tools:
                raise ValueError(f"source does not expose the requested tool: {tool_name}")
            arguments: dict[str, object] = {}
            if tool_name == "institution.attendance_summary":
                arguments["limit"] = 3
            planned.append(PlannedTool(name=tool_name, source_id=source_id, arguments=arguments))

    if len(planned) > 5:
        raise ValueError("plan exceeds the maximum tool-step bound")
    return tuple(planned)


__all__ = ["PlannedTool", "build_tool_plan", "classify_prompt", "is_public_web_prompt"]
