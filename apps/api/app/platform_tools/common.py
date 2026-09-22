"""Shared helpers for tool handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..gateway.spec import ToolOutput


def provenance(institution_id: str, source: str, *, rows_used: int = 0, complete: bool = True, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": f"{institution_id}:{source}", "source_type": "internal_database", "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "rows_used": rows_used, "complete": complete,
    }
    if extra:
        payload.update(extra)
    return payload


def output(institution_id: str, source: str, data: Any, summary: str, *, rows_used: int = 0, records_returned: int | None = None, warnings: list[dict[str, str]] | None = None, artifacts: list[dict[str, Any]] | None = None, complete: bool = True) -> ToolOutput:
    return ToolOutput(
        data=data, summary=summary, warnings=list(warnings or []), provenance=[provenance(institution_id, source, rows_used=rows_used, complete=complete)],
        records_returned=records_returned if records_returned is not None else rows_used, artifacts=list(artifacts or []),
    )


def describe_filters(filters: dict[str, Any]) -> str:
    parts = [f"{key.replace('_', ' ')} {value}" for key, value in filters.items() if value not in (None, "", [])]
    return " for " + ", ".join(parts) if parts else ""


__all__ = ["describe_filters", "output", "provenance"]
