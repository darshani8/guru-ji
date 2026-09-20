"""Connector protocol: driver details stop at this boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.principals import Capability, InstitutionScope, PrincipalType
from ..domain.results import ResultStatus, ToolResult
from ..domain.source_health import SourceHealth
from ..policy.query_limits import QueryLimits


@dataclass(frozen=True, slots=True)
class ConnectorContext:
    request_id: str
    source_id: str
    limits: QueryLimits
    principal_id: str | None = None
    principal_type: PrincipalType | None = None
    institution_scope: InstitutionScope | None = None
    capabilities: frozenset[Capability] = frozenset()
    consent_verified: bool = False
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.source_id.strip():
            raise ValueError("connector context identifiers must not be blank")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


class ReadOnlyConnector(Protocol):
    @property
    def source_id(self) -> str: ...

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
