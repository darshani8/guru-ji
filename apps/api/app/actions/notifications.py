"""In-app notifications with a control-plane outbox hook for push/email fan-out."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..domain.principals import Capability, InstitutionScope, Principal
from ..institution_data.store import InstitutionDataStore
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore

MAX_NOTIFICATION_RECIPIENTS = 200


@dataclass(slots=True)
class NotificationService:
    store: InstitutionDataStore
    control_store: InMemoryControlStore | PostgresControlStore | SqliteControlStore | None = None

    def create(self, principal: Principal, institution_id: str, *, recipient_ids: Sequence[str], title: str, body: str, reference_type: str | None = None, reference_id: str | None = None) -> dict[str, Any]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("notification scope is outside the caller's institution")
        if not principal.has_capability(Capability.ACTIONS_NOTIFY):
            raise PermissionError("actions:notify capability is required")
        if not title.strip() or not body.strip():
            raise ValueError("notification title and body must not be blank")
        recipients = list(dict.fromkeys(str(item).strip() for item in recipient_ids if str(item).strip()))[:MAX_NOTIFICATION_RECIPIENTS]
        if not recipients:
            recipients = [principal.principal_id]
        created: list[str] = []
        for recipient in recipients:
            notification_id = self.store.add_notification(institution_id, recipient_id=recipient, title=title.strip()[:200], body=body.strip()[:4000], created_by=principal.principal_id, reference_type=reference_type, reference_id=reference_id)
            created.append(notification_id)
        if self.control_store is not None:
            self.control_store.enqueue_outbox("notification.created", {"institution_id": institution_id, "recipients": recipients, "notification_ids": created, "title": title.strip()[:200]})
        return {"notification_ids": created, "recipients": recipients, "count": len(created)}

    def system_notify(self, institution_id: str, *, recipient_ids: Sequence[str], title: str, body: str, created_by: str = "system", reference_type: str | None = None, reference_id: str | None = None) -> list[str]:
        """Platform-originated notifications (job completion, monitoring alerts)."""

        created = [
            self.store.add_notification(institution_id, recipient_id=recipient, title=title[:200], body=body[:4000], created_by=created_by, reference_type=reference_type, reference_id=reference_id)
            for recipient in dict.fromkeys(str(item) for item in recipient_ids if str(item).strip())
        ]
        if self.control_store is not None and created:
            self.control_store.enqueue_outbox("notification.created", {"institution_id": institution_id, "recipients": list(recipient_ids), "notification_ids": created, "title": title[:200]})
        return created

    def inbox(self, principal: Principal, institution_id: str, *, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("notification scope is outside the caller's institution")
        return self.store.list_notifications(institution_id, principal.principal_id, limit=limit, unread_only=unread_only)

    def mark_read(self, principal: Principal, institution_id: str, notification_id: str) -> bool:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("notification scope is outside the caller's institution")
        return self.store.mark_notification_read(institution_id, principal.principal_id, notification_id)


__all__ = ["NotificationService"]
