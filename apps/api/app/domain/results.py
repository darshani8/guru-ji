"""Typed outcomes returned by semantic tools and connectors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .provenance import Provenance, Warning


class ResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    STALE = "stale"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESULT = "invalid_result"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A bounded connector result; arbitrary driver objects never leave this boundary."""

    tool_name: str
    status: ResultStatus
    data: object = None
    provenance: tuple[Provenance, ...] = ()
    warnings: tuple[Warning, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be blank")
        status = self.status
        if isinstance(status, str):
            status = ResultStatus(status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "warnings", tuple(self.warnings))


__all__ = ["ResultStatus", "ToolResult"]
