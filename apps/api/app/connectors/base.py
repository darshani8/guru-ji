"""Connector protocol: driver details stop at this boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.provenance import Provenance
from ..domain.results import ResultStatus, ToolResult
from ..domain.source_health import SourceHealth
from ..policy.query_limits import QueryLimits


@dataclass(frozen=True, slots=True)
class ConnectorContext:
    request_id: str
    source_id: str
    limits: QueryLimits


class ReadOnlyConnector(Protocol):
    source_id: str

    async def health(self) -> SourceHealth: ...

    async def execute(self, tool_name: str, arguments: dict[str, object], context: ConnectorContext) -> ToolResult: ...


def unavailable_result(tool_name: str, source_id: str, message: str) -> ToolResult:
    from ..domain.provenance import Warning

    return ToolResult(
        tool_name=tool_name,
        status=ResultStatus.UNAVAILABLE,
        warnings=(Warning(code="source_unavailable", message=message, source_id=source_id),),
    )


__all__ = ["ConnectorContext", "ReadOnlyConnector", "unavailable_result"]
