import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import CourtRecordsConnector
from app.internet_intelligence.map.connectors.base import ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.gate import compare
from app.internet_intelligence.map.metrics import map_metrics
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.search import StaticSearchProvider, hits_from_fixture
from app.internet_intelligence.service import InternetIntelligenceService, InvestigationQuotaExceeded
from app.internet_intelligence.store import IntelligenceStore
from platform_fixtures import principal
from test_intelligence_map_connectors import api

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
INSTITUTION = "bgscet"
PROFILE = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, PROFILE)["entity_id"]
        self.service = MapService(self.store)
        self.manager = principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION)
        self.reader = principal(PrincipalType.FACULTY, college_id=INSTITUTION)

    def account(self, url, relation="unknown"):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation=relation)
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="search_snippet", channel="search:t", observed_via="index")
        regrade(self.store, INSTITUTION, [asset_id])
        return asset_id

    def queue(self, asset_id, kind="impersonation_candidate"):
        review_id, outcome = self.store.add_review_item(INSTITUTION, kind=kind, title="check this", asset_id=asset_id, url=self.store.get_asset(INSTITUTION, asset_id)["url"])
        self.assertEqual(outcome, "added")
        return review_id


class QueueStoreTests(Base):
    def test_items_are_queued_once_capped_and_expire(self):
        first = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/1/")
        again = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET (re-found)", url="https://indiankanoon.org/doc/1/")
        self.assertEqual((first[0], again), (first[0], (first[0], "exists")))
        with self.assertRaises(ValueError):
            self.store.add_review_item(INSTITUTION, kind="gossip", title="x")
        with mock.patch("app.internet_intelligence.map.store.MAX_OPEN_REVIEW", 1):
            self.assertEqual(self.store.add_review_item(INSTITUTION, kind="court_record", title="another", url="https://indiankanoon.org/doc/2/"), (None, "full"))
        self.assertEqual(self.store.review_counts(INSTITUTION), {"court_record": 1})
        self.assertEqual(self.store.expire_review_items(INSTITUTION, now=(NOW + timedelta(days=400)).isoformat()), 1)
        self.assertEqual(self.store.list_review_items(INSTITUTION), [])
        self.assertEqual(self.store.list_review_items("other", status=None), [], "the queue is per institution")


class DecisionTests(Base):
    def test_confirm_reject_and_the_rules_around_them(self):
        confirmed = self.account("https://www.instagram.com/bgscet_mba/")
        rejected = self.account("https://www.instagram.com/bgscet.official/")
        self.store.upsert_source(INSTITUTION, connector="lead_page", target="https://www.instagram.com/bgscet.official/", asset_id=rejected, origin="lead", work_class="explore")
        first, second = self.queue(confirmed), self.queue(rejected)
        with self.assertRaises(PermissionError):
            self.service.review_items(self.reader, INSTITUTION)
        with self.assertRaises(PermissionError):
            self.service.decide(self.reader, INSTITUTION, first, decision="confirm")
        with self.assertRaisesRegex(ValueError, "takes one of"):
            self.service.decide(self.manager, INSTITUTION, first, decision="publish")
        result = self.service.decide(self.manager, INSTITUTION, first, decision="confirm", note="the MBA office's account", relation="official")
        asset = self.store.get_asset(INSTITUTION, confirmed)
        self.assertEqual((asset["grade"], asset["relation"]), ("B", "official"), "a reviewer's word is B, never A")
        self.assertEqual(result["changes"][0]["to"], "B")
        with self.assertRaisesRegex(ValueError, "already decided"):
            self.service.decide(self.manager, INSTITUTION, first, decision="reject")
        result = self.service.decide(self.manager, INSTITUTION, second, decision="impersonation", note="not ours; copies our logo")
        self.assertEqual(self.store.get_asset(INSTITUTION, rejected)["grade"], "D")
        self.assertEqual(result["incident"]["kind"], "impersonation_confirmed")
        self.assertEqual(result["sources_pruned"], 1)
        evidence = self.store.list_evidence(INSTITUTION, asset_id=rejected)
        self.assertEqual((evidence[-1]["kind"], evidence[-1]["observed_via"], evidence[-1]["channel"]), ("impersonation", "reviewer", f"reviewer:{self.manager.principal_id}"))
        with self.assertRaises(KeyError):
            self.service.decide(self.manager, INSTITUTION, "irev-missing", decision="confirm")

    def test_a_personal_account_leaves_no_trace(self):
        person = self.account("https://www.instagram.com/rahul.k/")
        self.store.upsert_source(INSTITUTION, connector="youtube", target=person, asset_id=person, origin="recurring")
        review_id = self.queue(person, kind="candidate_account")
        self.service.decide(self.manager, INSTITUTION, review_id, decision="personal", note="a student's own account")
        self.assertIsNone(self.store.get_asset(INSTITUTION, person))
        self.assertEqual(self.store.list_evidence(INSTITUTION, asset_id=person), [])
        self.assertEqual(self.store.list_sources(INSTITUTION, connector="youtube"), [])
        item = self.store.get_review_item(INSTITUTION, review_id)
        self.assertEqual((item["url"], item["asset_id"], item["decision"]), ("", None, "personal"))
        self.assertNotIn("rahul", item["title"])
        self.assertTrue(self.store.is_suppressed(INSTITUTION, "instagram:rahul.k"), "it can never be added again")

    def test_court_records_are_acknowledged_not_graded(self):
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/1/")
        with self.assertRaisesRegex(ValueError, "acknowledge"):
            self.service.decide(self.manager, INSTITUTION, review_id, decision="confirm")
        self.assertEqual(self.service.decide(self.manager, INSTITUTION, review_id, decision="acknowledge")["kind"], "court_record")


class GateTests(Base):
    def test_a_known_lookalike_that_reaches_b_is_set_back_and_reported(self):
        self.store.add_gold(INSTITUTION, asset_key="web:bgsgroupofinstitutions.com", platform="website", split="canary", entity_name="BGS Group of Institutions", relation="third_party", expected_min_grade="D")
        leak, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.bgsgroupofinstitutions.com/"), entity_id=None)
        for channel in ("search:tavily", "wikidata"):
            self.store.add_evidence(INSTITUTION, asset_id=leak, kind="search_snippet" if channel.startswith("search") else "community_record", channel=channel, observed_via="index")

        class Touch:
            name, access_mode, budget_key, max_grade, default_interval = "touch", "public_page", "fetch", "C", 86400

            def enabled(self):
                return True

            def cost(self, source):
                return 0

            async def run(self, source, context):
                return ConnectorResult(outcome="ok", touched={leak}, yield_count=1)

        self.store.upsert_source(INSTITUTION, connector="touch", target="t", due_at=NOW.isoformat())
        result = asyncio.run(MapEngine(self.store, ConnectorRegistry([Touch()]), clock=lambda: NOW).tick(INSTITUTION))
        self.assertEqual(self.store.get_asset(INSTITUTION, leak)["grade"], "D", "two channels made it B; ground truth sets it back")
        self.assertEqual([incident["kind"] for incident in result["incidents"]], ["canary_leak"])
        self.assertEqual(self.store.list_map_runs(INSTITUTION, kind="tick")[0]["gate"], "canary_held")
        self.assertEqual([item["kind"] for item in self.store.list_review_items(INSTITUTION)], ["canary_leak"])
        self.assertEqual(map_metrics(self.store, INSTITUTION)["canary_leaks"], [])

    def test_a_rescore_that_loses_ground_truth_waits_for_a_person(self):
        stale = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=self.entity_id, relation="official")[0]
        # Graded B under an older rule, with no evidence the current rule accepts.
        self.store.set_grade(INSTITUTION, stale, grade="B", reasons=["old rule"], scorer_version="grader-0")
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgscet_engg_coll", platform="instagram", split="seed", expected_min_grade="B")
        held = self.service.rescore(self.manager, INSTITUTION)
        self.assertFalse(held["passed"])
        self.assertIn("seed verification would fall", held["reasons"][0])
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["grade"], "B", "nothing is published while held")
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["proposed_grade"], "unrated")
        [gate] = self.store.list_review_items(INSTITUTION, kind="run_gate")
        self.service.decide(self.manager, INSTITUTION, gate["review_id"], decision="discard")
        self.assertIsNone(self.store.get_asset(INSTITUTION, stale)["proposed_grade"])
        # With evidence the rule accepts, the same rescore passes and publishes.
        self.store.add_evidence(INSTITUTION, asset_id=stale, kind="reviewer_confirm", channel="reviewer:x", observed_via="reviewer")
        passed = self.service.rescore(self.manager, INSTITUTION)
        self.assertTrue(passed["passed"])
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["grade"], "B")
        self.assertEqual(self.store.list_map_runs(INSTITUTION, kind="rescore")[0]["gate"], "passed")

    def test_the_comparison(self):
        base = {"canary_leaks": [], "seed_verification_rate": 0.8, "holdout_recall": 0.5}
        self.assertTrue(compare(base, {**base, "seed_verification_rate": 0.79}).passed, "within tolerance")
        self.assertFalse(compare(base, {**base, "holdout_recall": 0.4}).passed)
        self.assertFalse(compare(base, {**base, "canary_leaks": [{"asset_key": "web:x"}]}).passed)
        self.assertTrue(compare({**base, "canary_leaks": [{"asset_key": "web:x"}]}, {**base, "canary_leaks": [{"asset_key": "web:x"}]}).passed, "only new leaks block")


class EngineQueueTests(Base):
    def test_court_records_are_queued_once_across_ticks(self):
        client = api({("api.indiankanoon.org", "/search/"): (200, {"docs": [{"tid": 7, "title": "BGS College of Engineering and Technology v. State", "headline": ""}]})})
        clock = [NOW]
        engine = MapEngine(self.store, ConnectorRegistry([CourtRecordsConnector(api_token="t", active=True, client=client)]), clock=lambda: clock[0])
        first = asyncio.run(engine.tick(INSTITUTION))
        clock[0] = NOW + timedelta(days=31)
        second = asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual((first["counts"]["review"], second["counts"]["review"]), (1, 0), "found again, not asked again")
        [item] = self.store.list_review_items(INSTITUTION)
        self.assertEqual((item["kind"], item["connector"], item["url"]), ("court_record", "court_records", "https://indiankanoon.org/doc/7/"))


CONTROVERSY = [
    {"url": "https://citynews.example.com/abc-protest", "title": "Students protest fee hike at ABC College", "snippet": "Students of ABC College, Bengaluru staged a protest; a complaint was filed.", "published_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    {"url": "https://citynews.example.com/abc-fest", "title": "ABC College fest draws crowds", "snippet": "The annual fest at ABC College, Bengaluru was held.", "published_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
]
ABC = InstitutionProfile("college_a", "ABC College", "Bengaluru", monitoring_enabled=True)


class ReaderAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = IntelligenceStore(":memory:")
        self.service = InternetIntelligenceService(self.store, StaticSearchProvider(hits_from_fixture(CONTROVERSY)), fetcher=None, investigations_per_day=2)
        self.manager = principal(PrincipalType.PRINCIPAL)
        self.reader = principal(PrincipalType.FACULTY)
        self.service.save_profile(self.manager, "college_a", ABC.as_dict())

    async def test_sensitive_findings_reach_managers_first(self):
        managed = await self.service.investigate(self.manager, "college_a", window_days=7)
        self.assertIn("https://citynews.example.com/abc-protest", [item["url"] for item in managed["findings"]])
        read = await self.service.investigate(self.reader, "college_a", window_days=7)
        self.assertEqual([item["url"] for item in read["findings"]], ["https://citynews.example.com/abc-fest"])
        self.assertIn("held_for_review", {warning["code"] for warning in read["warnings"]})
        self.assertEqual(read["review_candidates"], [])
        self.assertNotIn("protest", read["summary"].lower())
        self.assertEqual(len(self.store.list_reports("college_a")), 2)
        self.assertEqual(len(self.store.list_reports("college_a", include_sensitive=False)), 1, "the manager's report mentions the protest")
        kept = {row["url"] for row in self.store.list_documents("college_a", exclude_sensitive=True)}
        self.assertEqual(kept, {"https://citynews.example.com/abc-fest"})

    async def test_each_person_has_a_daily_allowance(self):
        await self.service.investigate(self.reader, "college_a", window_days=7)
        await self.service.investigate(self.reader, "college_a", window_days=7)
        with self.assertRaises(InvestigationQuotaExceeded):
            await self.service.investigate(self.reader, "college_a", window_days=7)
        other = principal(PrincipalType.FACULTY, principal_id="faculty-2")
        await self.service.investigate(other, "college_a", window_days=7)
        self.assertEqual(self.store.investigations_used("college_a", self.reader.principal_id, day=datetime.now(timezone.utc).date().isoformat()), 2)

    async def test_the_digest_hides_sensitive_events_from_readers(self):
        from app.internet_intelligence.monitoring import ContinuousMonitor

        await ContinuousMonitor(self.service, self.store, window_days=7).run_for("college_a")
        self.assertEqual(self.service.digest(self.manager, "college_a", days=1)["total_items"], 2)
        reader_digest = self.service.digest(self.reader, "college_a", days=1)
        self.assertEqual(reader_digest["total_items"], 1)
        self.assertNotIn("protest", reader_digest["summary"].lower())


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class ReviewRouteTests(unittest.TestCase):
    def test_the_queue_routes_are_for_managers(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "rev-principal", "X-Demo-Role": "principal", "X-Demo-College": "rev_college"}
        faculty = {**headers, "X-Demo-Principal": "rev-faculty", "X-Demo-Role": "faculty"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        asset_id, _ = store.upsert_asset("rev_college", asset_ref("https://www.instagram.com/rev_college_official/"), entity_id=None)
        review_id, _ = store.add_review_item("rev_college", kind="impersonation_candidate", title="calls itself official", asset_id=asset_id, url="https://www.instagram.com/rev_college_official/")
        with mock.patch.object(platform, "intelligence_map", MapService(store)):
            self.assertEqual(client.get("/v1/intelligence/map/review", headers=faculty).status_code, 403)
            listed = client.get("/v1/intelligence/map/review", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["review_id"] for item in listed.json()["items"]], [review_id])
            self.assertEqual(client.post(f"/v1/intelligence/map/review/{review_id}/decision", headers=headers, json={"decision": "publish"}).status_code, 422)
            self.assertEqual(client.post("/v1/intelligence/map/review/irev-nope/decision", headers=headers, json={"decision": "reject"}).status_code, 404)
            decided = client.post(f"/v1/intelligence/map/review/{review_id}/decision", headers=headers, json={"decision": "reject", "note": "not ours"})
            self.assertEqual(decided.status_code, 200, decided.text)
            self.assertEqual(store.get_asset("rev_college", asset_id)["grade"], "D")
            self.assertEqual(client.post("/v1/intelligence/map/rescore", headers=headers, json={}).json()["passed"], True)
            events = {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)}
            self.assertTrue({"intelligence.map.review_decision", "intelligence.map.rescore"} <= events)

    def test_readers_get_only_kept_mentions(self):
        client = TestClient(app)
        faculty = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "men-faculty", "X-Demo-Role": "faculty", "X-Demo-College": "men_college"}
        self.assertEqual(client.get("/v1/intelligence/mentions?status=review", headers=faculty).status_code, 403)
        ok = client.get("/v1/intelligence/mentions", headers=faculty)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["sensitive_hidden"])


if __name__ == "__main__":
    unittest.main()
