"""Behaviour only a real PostgreSQL shows: run-lock races, typed parameters, re-runnable migrations.

Skipped unless GURU_TEST_POSTGRES_URL names a throwaway database the tests
may create tables in, for example
``GURU_TEST_POSTGRES_URL=postgresql://guru:guru@127.0.0.1:55432/guru``. Use a
role without BYPASSRLS so row-level security applies as in production.
"""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from uuid import uuid4

POSTGRES_URL = os.environ.get("GURU_TEST_POSTGRES_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(POSTGRES_URL, "set GURU_TEST_POSTGRES_URL to run the PostgreSQL tests")
class PostgresRunLockTests(unittest.TestCase):
    def _race(self, first_store, second_store, start_run):
        """Start a run inside an open transaction, then try again from another connection.

        The second attempt must wait for the first transaction and then see
        its run; under READ COMMITTED a plain NOT EXISTS check would not.
        """

        institution = f"pgt-{uuid4().hex[:10]}"
        outcome: dict[str, object] = {}
        with first_store.backend.transaction(tenant_id=institution):
            outcome["first"] = start_run(first_store, institution)
            thread = threading.Thread(target=lambda: outcome.setdefault("second", start_run(second_store, institution)))
            thread.start()
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive(), "the second start waits for the first transaction")
        thread.join(timeout=15)
        self.assertIsNotNone(outcome["first"])
        self.assertIsNone(outcome["second"], "exactly one run starts")

    def test_monitoring_runs_do_not_overlap(self):
        from app.internet_intelligence.store import IntelligenceStore

        self._race(IntelligenceStore(POSTGRES_URL), IntelligenceStore(POSTGRES_URL), lambda store, institution: store.try_start_run(institution))

    def test_map_ticks_do_not_overlap(self):
        from app.internet_intelligence.map.store import MapStore

        self._race(
            MapStore(POSTGRES_URL, suppression_key=b"k"), MapStore(POSTGRES_URL, suppression_key=b"k"),
            lambda store, institution: store.try_start_map_run(institution, kind="tick", lock_seconds=3600),
        )


@unittest.skipUnless(POSTGRES_URL, "set GURU_TEST_POSTGRES_URL to run the PostgreSQL tests")
class PostgresMapStoreTests(unittest.TestCase):
    def test_observations_with_only_some_fields(self):
        from app.internet_intelligence.map.assets import asset_ref
        from app.internet_intelligence.map.store import MapStore

        store = MapStore(POSTGRES_URL, suppression_key=b"k")
        institution = f"pgt-{uuid4().hex[:10]}"
        asset_id, _ = store.upsert_asset(institution, asset_ref("https://www.youtube.com/channel/UClACJA8pf4QbkdH4__7MTGA"), entity_id=None)
        store.set_observation(institution, asset_id, platform_id="p1")
        store.set_observation(institution, asset_id, followers=12, followers_source="youtube_api")
        store.set_observation(institution, asset_id, last_activity_at="2026-09-01T00:00:00+00:00")
        asset = store.get_asset(institution, asset_id)
        self.assertEqual((asset["platform_id"], asset["followers"], asset["followers_source"], asset["last_activity_at"][:10]), ("p1", 12, "youtube_api", "2026-09-01"))

    def test_the_review_queue_and_its_decisions(self):
        from app.internet_intelligence.map.assets import asset_ref
        from app.internet_intelligence.map.store import MapStore

        store = MapStore(POSTGRES_URL, suppression_key=b"k")
        institution = f"pgt-{uuid4().hex[:10]}"
        asset_id, _ = store.upsert_asset(institution, asset_ref("https://www.instagram.com/someone/"), entity_id=None)
        store.upsert_source(institution, connector="lead_page", target="https://www.instagram.com/someone/", asset_id=asset_id, origin="lead", work_class="explore")
        review_id, outcome = store.add_review_item(institution, kind="candidate_account", title="someone", asset_id=asset_id, url="https://www.instagram.com/someone/")
        self.assertEqual((outcome, store.add_review_item(institution, kind="candidate_account", title="someone", asset_id=asset_id, url="https://www.instagram.com/someone/")[1]), ("added", "exists"))
        self.assertEqual(store.prune_sources_for(institution, asset_id=asset_id, url="https://www.instagram.com/someone/"), 1)
        self.assertTrue(store.decide_review_item(institution, review_id, decision="personal", decided_by="p", redact=True))
        self.assertTrue(store.forget_asset(institution, asset_id))
        self.assertEqual(store.review_counts(institution), {})
        self.assertEqual(store.apply_proposed(institution), 0)
        self.assertEqual(store.list_review_items(institution, status=None)[0]["url"], "")

    def test_incidents_recur_and_reopen(self):
        from app.internet_intelligence.map.incidents import IncidentDesk
        from app.internet_intelligence.map.store import MapStore

        store = MapStore(POSTGRES_URL, suppression_key=b"k")
        institution = f"pgt-{uuid4().hex[:10]}"
        sent: list[str] = []
        desk = IncidentDesk(store, recipients=lambda _: ("p",), notify=lambda *args: sent.append(args[4]))
        self.assertEqual(desk.record(institution, [{"kind": "site_hijacked", "target": "x.in", "run_id": None}])["notified"], 1)
        self.assertEqual(desk.record(institution, [{"kind": "site_hijacked", "target": "x.in"}], run_id="r2")["repeat"], 1)
        [incident] = store.list_incidents(institution)
        self.assertTrue(store.set_incident_status(institution, incident["incident_id"], status="resolved", by="p"))
        self.assertEqual(desk.record(institution, [{"kind": "site_hijacked", "target": "x.in"}])["reopened"], 1)
        self.assertEqual(len(sent), 2)
        self.assertTrue(desk.send_digest(institution)["sent"])

    def test_the_shared_cache_is_global(self):
        from app.internet_intelligence.map.store import MapStore

        store = MapStore(POSTGRES_URL, suppression_key=b"k")
        self.addCleanup(store.close)
        key = f"pgt-{uuid4().hex}"
        with store.batch(f"pgt-{uuid4().hex[:10]}"):
            store.cache_put("search", key, [{"url": "https://example.org/"}], 3600)
        with store.batch(f"pgt-{uuid4().hex[:10]}"):
            self.assertEqual(store.cache_get("search", key), [{"url": "https://example.org/"}], "another institution reads the same public answer")
        store.cache_put("search", key, [], 0)
        self.assertIsNone(store.cache_get("search", key), "an upsert with no time left has expired")
        self.assertGreaterEqual(store.cache_prune(), 1)
        row = store.backend.fetchone("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'intel_shared_cache' AND relnamespace = current_schema()::regnamespace")
        self.assertEqual((row["relrowsecurity"], row["relforcerowsecurity"]), (False, False), "public-web data, outside row-level security")

    def test_investigation_quota(self):
        from app.internet_intelligence.store import IntelligenceStore

        store = IntelligenceStore(POSTGRES_URL)
        institution = f"pgt-{uuid4().hex[:10]}"
        self.assertEqual([store.take_investigation(institution, "p1", day="2026-09-22", cap=2) for _ in range(3)], [True, True, False])
        self.assertEqual(store.investigations_used(institution, "p1", day="2026-09-22"), 2)

    def test_migration_files_can_be_applied_twice(self):
        import psycopg

        from app.internet_intelligence.map.store import MapStore
        from app.internet_intelligence.store import IntelligenceStore

        # The application's own start-up has created everything first, as in a real deployment.
        IntelligenceStore(POSTGRES_URL)
        MapStore(POSTGRES_URL, suppression_key=b"k")
        with psycopg.connect(POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")) as connection:
            try:
                for name in ("003_internet_intelligence.sql", "004_intelligence_map.sql"):
                    script = (REPO_ROOT / "migrations" / name).read_text(encoding="utf-8")
                    connection.execute(script)
                    connection.execute(script)
            finally:
                connection.rollback()


if __name__ == "__main__":
    unittest.main()
