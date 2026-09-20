"""Reliable control-plane outbox dispatch helpers."""

from __future__ import annotations

from collections.abc import Callable

from .control_plane import OutboxRecord
from .database import InMemoryControlStore, PostgresControlStore, SqliteControlStore


ControlStore = InMemoryControlStore | SqliteControlStore | PostgresControlStore
OutboxHandler = Callable[[OutboxRecord], None]


class OutboxDispatcher:
    def __init__(self, store: ControlStore, handler: OutboxHandler) -> None:
        self.store = store
        self.handler = handler

    def dispatch_once(self, limit: int = 100) -> int:
        records = self.store.drain_outbox(limit)
        acknowledged: list[str] = []
        for record in records:
            self.handler(record)
            acknowledged.append(record.outbox_id)
        self.store.ack_outbox(tuple(acknowledged))
        return len(acknowledged)


__all__ = ["OutboxDispatcher"]
