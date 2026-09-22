"""Smoke-test the PostgreSQL control-plane store against a live database URL."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from app.domain.audit import AuditEvent
from app.persistence.control_plane import AnswerEnvelopeMetadata, ModelAttemptMetadata
from app.persistence.database import PostgresControlStore
from app.persistence.outbox import OutboxDispatcher


def main() -> int:
    url = os.getenv("CONTROL_DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("CONTROL_DATABASE_URL must be a PostgreSQL URL")
    store = PostgresControlStore(url)
    try:
        now = datetime.now(timezone.utc)
        store.append_audit(AuditEvent(
            event_id="postgres-smoke-audit",
            event_type="postgres.smoke",
            request_id="postgres-smoke-request",
            occurred_at=now,
        ))
        store.record_answer_envelope(AnswerEnvelopeMetadata(
            request_id="postgres-smoke-request",
            conversation_id="postgres-smoke-conversation",
            principal_id="postgres-smoke-principal",
            college_id="college_a",
            status="complete",
            citations_count=0,
            warnings_count=0,
            answer_sha256="0" * 64,
            created_at=now,
        ))
        store.record_model_attempt(ModelAttemptMetadata(
            request_id="postgres-smoke-request",
            provider_id="smoke",
            model_id="smoke",
            outcome="success",
            latency_ms=1,
        ))
        store.enqueue_outbox("postgres.smoke", {"request_id": "postgres-smoke-request"})
        handled: list[str] = []
        delivered = OutboxDispatcher(store, lambda record: handled.append(record.outbox_id)).dispatch_once()
        if delivered != 1 or len(handled) != 1 or store.drain_outbox():
            raise SystemExit("outbox acknowledgement failed")
        if not store.ping():
            raise SystemExit("postgres ping failed")
        store.prune_retention(365)
        print("POSTGRES_SMOKE_OK")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
