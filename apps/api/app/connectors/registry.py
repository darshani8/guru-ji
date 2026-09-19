"""Source-to-connector registry."""

from __future__ import annotations

from collections.abc import Iterable

from .base import ReadOnlyConnector


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[ReadOnlyConnector] = ()) -> None:
        self._connectors = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: ReadOnlyConnector) -> None:
        if connector.source_id in self._connectors:
            raise ValueError(f"connector already registered: {connector.source_id}")
        self._connectors[connector.source_id] = connector

    def get(self, source_id: str) -> ReadOnlyConnector:
        try:
            return self._connectors[source_id]
        except KeyError as exc:
            raise KeyError(f"connector not registered: {source_id}") from exc

    def all(self) -> tuple[ReadOnlyConnector, ...]:
        return tuple(self._connectors.values())


__all__ = ["ConnectorRegistry"]
