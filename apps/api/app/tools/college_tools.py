"""Read-only semantic tools for institution data."""

from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel
from .base import ToolDefinition


COLLEGE_TOOLS = (
    ToolDefinition(
        name="institution.overview",
        description="Return aggregate institution counts and headline indicators.",
        required_capability=Capability.ASK_READ_ONLY,
        source_ids=("college_a_demo",),
        classification=DataClassification.CONFIDENTIAL,
        disclosure_level=DisclosureLevel.AGGREGATE,
    ),
    ToolDefinition(
        name="institution.attendance_summary",
        description="Return aggregate attendance indicators by department.",
        required_capability=Capability.ASK_READ_ONLY,
        source_ids=("college_a_demo",),
        classification=DataClassification.CONFIDENTIAL,
        disclosure_level=DisclosureLevel.AGGREGATE,
    ),
)


__all__ = ["COLLEGE_TOOLS"]
