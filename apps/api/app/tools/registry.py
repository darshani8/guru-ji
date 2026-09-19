"""Central tool registry."""

from __future__ import annotations

from .base import ToolDefinition


class ToolRegistry:
    def __init__(self, tools: tuple[ToolDefinition, ...] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"tool not registered: {name}") from exc

    def all(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


__all__ = ["ToolRegistry"]
