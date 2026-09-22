"""Smoke-test the PostgreSQL control-plane store against a live database URL.

Besides a round trip through every table, this checks the two start-up
properties a rolling deployment depends on: a read leaves no transaction open
on the store's connection, and a new instance starts while an older
connection sits idle in transaction holding read locks on the tables.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.domain.audit import AuditEvent
from app.persistence.control_plane import AnswerEnvelopeMetadata, ModelAttemptMetadata
from app.persistence.database import PostgresControlStore
from app.persistence.outbox import OutboxDispatcher


def _start_up_next_to_an_idle_reader(url: str) -> None:
    """A second store must start while another connection holds read locks in an open transaction."""

    import psycopg

    older = psycopg.connect(url)  # no autocommit: the transaction stays open after the reads
    try:
        with older.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM audit_events")
            cursor.execute("SELECT count(*) FROM source_health")
            cursor.fetchone()
        outcome: dict[str, object] = {}

        def start() -> None:
            began = time.monotonic()
            try:
                PostgresControlStore(url).close()
                outcome["seconds"] = round(time.monotonic() - began, 3)
            except Exception as exc:  # noqa: BLE001 - reported below
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive():
            raise SystemExit("start-up blocked behind an idle-in-transaction reader")
        if "error" in outcome:
            raise SystemExit(f"start-up failed next to an idle-in-transaction reader: {outcome['error']}")
        print(f"start-up next to an idle reader took {outcome['seconds']}s")
    finally:
        older.close()


def main() -> int:
    url = os.getenv("CONTROL_DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("CONTROL_DATABASE_URL must be a PostgreSQL URL")
    run_id = uuid4().hex[:12]
    request_id = f"postgres-smoke-request-{run_id}"
    store = PostgresControlStore(url)
    try:
        now = datetime.now(timezone.utc)
        store.append_audit(AuditEvent(
            event_id=f"postgres-smoke-audit-{run_id}",
            event_type="postgres.smoke",
            request_id=request_id,
            occurred_at=now,
        ))
        store.record_answer_envelope(AnswerEnvelopeMetadata(
            request_id=request_id,
            conversation_id=f"postgres-smoke-conversation-{run_id}",
            principal_id="postgres-smoke-principal",
            college_id="college_a",
            status="complete",
            citations_count=0,
            warnings_count=0,
            answer_sha256="0" * 64,
            created_at=now,
        ))
        store.record_model_attempt(ModelAttemptMetadata(
            request_id=request_id,
            provider_id="smoke",
            model_id="smoke",
            outcome="success",
            latency_ms=1,
        ))
        outbox_id = store.enqueue_outbox("postgres.smoke", {"request_id": request_id})
        handled: list[str] = []
        OutboxDispatcher(store, lambda record: handled.append(record.outbox_id)).dispatch_once()
        if outbox_id not in handled or any(record.outbox_id == outbox_id for record in store.drain_outbox()):
            raise SystemExit("outbox acknowledgement failed")
        if not any(event.request_id == request_id for event in store.recent_audit(5)):
            raise SystemExit("audit read-back failed")
        if store.in_transaction:
            raise SystemExit("a read left the store's connection idle in transaction")
        if not store.ping():
            raise SystemExit("postgres ping failed")
        store.prune_retention(365)
        _start_up_next_to_an_idle_reader(url)
        print("POSTGRES_SMOKE_OK")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
