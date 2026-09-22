"""Registry of platform tools available to agents, filtered per principal."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..domain.principals import Principal
from .spec import PlatformToolSpec


class PlatformToolRegistry:
    def __init__(self, tools: Iterable[PlatformToolSpec] = ()) -> None:
        self._tools: dict[str, PlatformToolSpec] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: PlatformToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"platform tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> PlatformToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"platform tool not registered: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> tuple[PlatformToolSpec, ...]:
        return tuple(self._tools.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def for_principal(self, principal: Principal) -> tuple[PlatformToolSpec, ...]:
        return tuple(tool for tool in self._tools.values() if principal.has_capability(tool.required_capability))

    def groups(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.group, []).append(tool.name)
        return {group: tuple(names) for group, names in grouped.items()}

    def describe(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        tools = self.for_principal(principal) if principal is not None else self.all()
        return [tool.json_schema() for tool in tools]


__all__ = ["PlatformToolRegistry"]
