"""Tool definitions are the only actions the planner may request."""

from __future__ import annotations

from dataclasses import dataclass

from ..config.source_registry import SourceRegistry
from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    required_capability: Capability
    source_ids: tuple[str, ...]
    classification: DataClassification = DataClassification.CONFIDENTIAL
    disclosure_level: DisclosureLevel = DisclosureLevel.AGGREGATE
    read_only: bool = True
    supports_department_scope: bool = False
    supports_batch_scope: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be blank")
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        if not self.read_only:
            raise ValueError("Agent Saffron only registers read-only tools")

    def allows_source(self, source_id: str) -> bool:
        return source_id in self.source_ids


def validate_tools_against_sources(tools: tuple[ToolDefinition, ...], sources: SourceRegistry) -> None:
    for tool in tools:
        for source_id in tool.source_ids:
            sources.get(source_id)


__all__ = ["ToolDefinition", "validate_tools_against_sources"]
