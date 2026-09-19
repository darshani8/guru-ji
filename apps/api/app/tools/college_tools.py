"""Read-only semantic tools for institution data."""

from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel
from .base import ToolDefinition


def build_college_tools(source_ids: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
    source_ids = tuple(source_ids)
    return (
        ToolDefinition(
            name="institution.overview",
            description="Return aggregate institution counts and headline indicators.",
            required_capability=Capability.ASK_READ_ONLY,
            source_ids=source_ids,
            classification=DataClassification.CONFIDENTIAL,
            disclosure_level=DisclosureLevel.AGGREGATE,
        ),
        ToolDefinition(
            name="institution.attendance_summary",
            description="Return aggregate attendance indicators by department.",
            required_capability=Capability.ASK_READ_ONLY,
            source_ids=source_ids,
            classification=DataClassification.CONFIDENTIAL,
            disclosure_level=DisclosureLevel.AGGREGATE,
        ),
    )


COLLEGE_TOOLS = build_college_tools(("college_a_demo",))


__all__ = ["COLLEGE_TOOLS", "build_college_tools"]
