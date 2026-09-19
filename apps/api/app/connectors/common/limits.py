"""Connector-side bounded argument validation."""

from ..base import ConnectorContext


def bounded_int(arguments: dict[str, object], key: str, default: int, maximum: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0 or value > maximum:
        raise ValueError(f"{key} must be between 0 and {maximum}")
    return value


def ensure_within_limits(context: ConnectorContext, requested_rows: int) -> None:
    if requested_rows > context.limits.max_rows:
        raise ValueError("requested rows exceed the configured connector limit")


__all__ = ["bounded_int", "ensure_within_limits"]
