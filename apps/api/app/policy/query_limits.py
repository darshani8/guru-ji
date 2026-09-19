"""Hard limits shared by planners and connectors."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryLimits:
    max_duration_ms: int = 10_000
    max_rows: int = 500
    max_response_bytes: int = 1_000_000
    max_tool_calls: int = 8
    max_parallel_sources: int = 5

    def __post_init__(self) -> None:
        for field_name in (
            "max_duration_ms", "max_rows", "max_response_bytes",
            "max_tool_calls", "max_parallel_sources",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")


__all__ = ["QueryLimits"]
