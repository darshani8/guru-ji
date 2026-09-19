"""Health and metadata tools."""

from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel
from .base import ToolDefinition


HEALTH_TOOLS = (
    ToolDefinition(
        name="institution.source_health",
        description="Read the health state of an approved institution source.",
        required_capability=Capability.VIEW_SOURCE_METADATA,
        source_ids=("college_a_demo",),
        classification=DataClassification.INTERNAL,
        disclosure_level=DisclosureLevel.AGGREGATE,
    ),
)


__all__ = ["HEALTH_TOOLS"]
