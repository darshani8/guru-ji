import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorRegistry
from app.internet_intelligence.map.connectors.search import PLATFORM_DOMAINS
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.learning import GAP_INTERVAL_SECONDS, coverage_estimate, gap_grid, prioritise_gaps
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from platform_fixtures import principal

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
INSTITUTION = "bgscet"


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru"))["entity_id"]

    def account(self, url, *evidence):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation="official")
        for kind, channel, detail in evidence:
            self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind=kind, channel=channel, detail=detail, observed_via="live" if kind in {"official_link", "hub_link"} else "index")
        regrade(self.store, INSTITUTION, [asset_id])
        return asset_id


class GridTests(Base):
    def test_cells_are_covered_only_at_b_or_better(self):
        self.account("https://www.instagram.com/bgscet_engg_coll/", ("official_link", "site:bgscet.ac.in", "A:footer"))
        self.account("https://www.youtube.com/@bgscet", ("search_snippet", "search:t", ""))
        grid = gap_grid(self.store, INSTITUTION)
        [row] = grid["rows"]
        self.assertEqual(row["cells"]["instagram"], {"grade": "A", "covered": True})
        self.assertEqual(row["cells"]["youtube"], {"grade": "C", "covered": False})
        self.assertEqual((grid["covered"], grid["cells"]), (1, 10))
        self.assertIn((self.entity_id, "youtube"), grid["gaps"])

    def test_gap_searches_go_first_and_come_round_sooner(self):
        self.account("https://www.instagram.com/bgscet_engg_coll/", ("official_link", "site:bgscet.ac.in", "A:footer"))
        for domain in PLATFORM_DOMAINS:
            self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|{domain}", origin="recurring", interval_seconds=14 * 86400, due_at=NOW.isoformat())
        self.assertEqual(prioritise_gaps(self.store, INSTITUTION, now=NOW), 9)
        sources = {row["target"].split("|")[1]: row for row in self.store.list_sources(INSTITUTION, connector="search")}
        self.assertEqual((sources["youtube.com"]["topic"], sources["youtube.com"]["interval_seconds"]), ("gap", GAP_INTERVAL_SECONDS))
        self.assertEqual((sources["instagram.com"]["topic"], sources["instagram.com"]["interval_seconds"]), ("", 14 * 86400), "a covered cell keeps its pace")
        first = self.store.claim_due(INSTITUTION, now=NOW.isoformat(), worker="w", lease_seconds=60, limit=1, order="priority")
        self.assertEqual(first[0]["topic"], "gap")


class PriorityTests(Base):
    def test_productive_sources_go_first_but_nothing_starves(self):
        stamp = (NOW - timedelta(days=1)).isoformat()
        productive, _ = self.store.upsert_source(INSTITUTION, connector="lead_page", target="https://good.example/", origin="lead", work_class="explore", due_at=NOW.isoformat())
        for index in range(4):
            self.store.upsert_source(INSTITUTION, connector="lead_page", target=f"https://old{index}.example/", origin="lead", work_class="explore", due_at=stamp)
        self.store.complete_source(INSTITUTION, productive, outcome="ok", next_due=NOW.isoformat(), interval_seconds=3600, yield_count=5, cost=1, failed=False)
        self.assertAlmostEqual(self.store.get_source(INSTITUTION, productive)["priority"], 1.5)
        engine = MapEngine(self.store, ConnectorRegistry(), EngineConfig(sources_per_tick=2, class_split=(("explore", 1.0),)), clock=lambda: NOW)
        claimed = [row["target"] for row in engine._claim(INSTITUTION, "w", connectors=["lead_page"], total=2)]
        self.assertEqual(claimed[0], "https://good.example/", "the productive source first")
        self.assertIn(claimed[1], {f"https://old{index}.example/" for index in range(4)}, "and the longest overdue next")
        self.store.complete_source(INSTITUTION, productive, outcome="ok", next_due=NOW.isoformat(), interval_seconds=3600, yield_count=0, cost=1, failed=False)
        self.assertAlmostEqual(self.store.get_source(INSTITUTION, productive)["priority"], 1.05, msg="the average decays when it stops finding things")
        yields = {row["connector"]: row for row in self.store.source_yields(INSTITUTION)}
        self.assertEqual((yields["lead_page"]["found"], yields["lead_page"]["runs"], yields["lead_page"]["found_per_run"]), (5, 2, 2.5))


class CoverageTests(Base):
    def test_capture_recapture(self):
        site = ("official_link", "site:bgscet.ac.in", "A:footer")
        index = ("search_snippet", "search:t", "")
        for name in ("s1", "s2", "s3", "s4"):
            self.account(f"https://www.instagram.com/{name}/", site)
        for name in ("i1", "i2", "i3"):
            self.account(f"https://www.instagram.com/{name}/", index)
        for name in ("b1", "b2"):
            self.account(f"https://www.instagram.com/{name}/", site, index)
        estimate = coverage_estimate(self.store, INSTITUTION)
        # n1 = 6 found through the site, n2 = 5 through indexes, m = 2 by both.
        self.assertEqual((estimate["known"], estimate["estimated_total"]), (9, 13.0))
        self.assertEqual(estimate["coverage"], round(9 / 13, 3))
        self.assertEqual(estimate["by_platform"]["instagram"]["both"], 2)
        self.assertIn("floor", estimate["assumption"])
        self.assertIsNone(coverage_estimate(MapStore(":memory:", suppression_key=b"k"), "empty")["estimated_total"], "no estimate without both captures")


class SummaryTests(Base):
    def test_readers_see_what_the_map_stands_behind(self):
        self.account("https://www.instagram.com/bgscet_engg_coll/", ("official_link", "site:bgscet.ac.in", "A:footer"))
        self.account("https://www.youtube.com/@bgscet", ("search_snippet", "search:t", ""))
        service = MapService(self.store)
        reader = service.summary(principal(PrincipalType.FACULTY, college_id=INSTITUTION), INSTITUTION)
        manager = service.summary(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION)
        self.assertNotIn("C", reader["by_grade"])
        self.assertIsNone(reader["grid"]["rows"][0]["cells"]["youtube"]["grade"], "an unverified account is not shown to readers, even as a grade")
        self.assertEqual(manager["grid"]["rows"][0]["cells"]["youtube"]["grade"], "C")
        for key in ("incidents_open", "review_waiting", "runs", "connectors", "spend_today", "ground_truth"):
            self.assertNotIn(key, reader)
            self.assertIn(key, manager)
        with self.assertRaises(PermissionError):
            service.summary(principal(PrincipalType.STUDENT, college_id=INSTITUTION), INSTITUTION)


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class SummaryRouteTests(unittest.TestCase):
    def test_summary_route(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "sum-principal", "X-Demo-Role": "principal", "X-Demo-College": "sum_college"}
        platform = app.state.runtime.platform
        with mock.patch.object(platform, "intelligence_map", MapService(MapStore(backend=platform.store.backend, suppression_key=b"test-key"))):
            managed = client.get("/v1/intelligence/map/summary", headers=headers)
            self.assertEqual(managed.status_code, 200)
            self.assertIn("coverage", managed.json())
            read = client.get("/v1/intelligence/map/summary", headers={**headers, "X-Demo-Principal": "sum-faculty", "X-Demo-Role": "faculty"})
            self.assertEqual(read.status_code, 200)
            self.assertNotIn("incidents_open", read.json())


if __name__ == "__main__":
    unittest.main()
