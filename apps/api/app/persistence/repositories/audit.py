"""Audit repository facade."""

from ..database import InMemoryControlStore


class AuditRepository:
    def __init__(self, store: InMemoryControlStore) -> None:
        self.store = store

    def recent(self, limit: int = 100):
        return self.store.recent_audit(limit)


__all__ = ["AuditRepository"]
