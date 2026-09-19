"""Combine bounded tool results without losing warnings or provenance."""

from ..domain.provenance import Provenance, Warning
from ..domain.results import ResultStatus, ToolResult


def aggregate(results: tuple[ToolResult, ...]) -> tuple[dict[str, object], tuple[Provenance, ...], tuple[Warning, ...], bool]:
    data: dict[str, object] = {}
    provenance: list[Provenance] = []
    warnings: list[Warning] = []
    partial = False
    for result in results:
        if result.status is not ResultStatus.SUCCESS:
            partial = True
        data[result.tool_name] = result.data
        provenance.extend(result.provenance)
        warnings.extend(result.warnings)
    return data, tuple(provenance), tuple(warnings), partial


__all__ = ["aggregate"]
