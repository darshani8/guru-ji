"""Health and metadata tools."""

from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel
from .base import ToolDefinition


def build_health_tools(source_ids: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="institution.source_health",
            description="Read the health state of an approved institution source.",
            required_capability=Capability.VIEW_SOURCE_METADATA,
            source_ids=tuple(source_ids),
            classification=DataClassification.INTERNAL,
            disclosure_level=DisclosureLevel.AGGREGATE,
        ),
    )


HEALTH_TOOLS = build_health_tools(("college_a_demo",))


__all__ = ["HEALTH_TOOLS", "build_health_tools"]
