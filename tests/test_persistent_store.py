from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.domain.audit import AuditEvent, AuditOutcome
from app.domain.source_health import Freshness, SourceHealth, SourceHealthStatus
from app.persistence.database import SqliteControlStore


class PersistentStoreTests(unittest.TestCase):
    def test_audit_health_and_briefing_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.db"
            url = f"sqlite:///{path.as_posix()}"
            first = SqliteControlStore(url)
            occurred = datetime.now(timezone.utc)
            first.append_audit(AuditEvent(event_id="evt-1", event_type="assistant.ask", request_id="req-1", occurred_at=occurred, principal_id="p-1", outcome=AuditOutcome.SUCCESS))
            first.set_health(SourceHealth(source_id="source-1", institution_id="college_a", status=SourceHealthStatus.HEALTHY, display_name="Source 1", connector_type="test", checked_at=occurred, last_success_at=occurred, freshness=Freshness.CURRENT, latency_ms=7, detail="ok"))
            first.record_briefing("brief-1", "p-1", "college_a", {"status": "complete"})
            first.close()

            second = SqliteControlStore(url)
            try:
                self.assertTrue(second.ping())
                self.assertEqual(second.recent_audit(1)[0].event_id, "evt-1")
                self.assertEqual(second.get_health("source-1").status, SourceHealthStatus.HEALTHY)  # type: ignore[union-attr]
                self.assertEqual(second.recent_briefings(1)[0]["briefing_id"], "brief-1")
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
