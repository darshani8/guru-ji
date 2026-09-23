"""The map's dashboard: the run series, precision, freshness, cost, the gap grid's reach and cadence, and what readers see."""

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.connectors.search import PLATFORM_DOMAINS, SearchConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.learning import GAP_INTERVAL_SECONDS, coverage_estimate, gap_grid, prioritise_gaps, ranked_entities
from app.internet_intelligence.map.metrics import map_metrics, record_baseline, run_series
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.seed import import_seed, load_default_seed
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
CONSOLE_JS = Path(__file__).resolve().parents[1] / "apps" / "web" / "console" / "console.js"


class NoHits:
    provider_name = "test"

    def __init__(self):
        self.calls = []

    async def search(self, query, max_results, include_domains):
        self.calls.append((query, tuple(include_domains)))
        return []


class Clock:
    def __init__(self, at=NOW):
        self.at = at

    def __call__(self):
        return self.at


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru"))["entity_id"]

    def asset(self, url, grade=None, *, entity_id=None, verified_at=None):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=entity_id or self.entity_id, relation="official")
        if grade:
            self.store.set_grade(INSTITUTION, asset_id, grade=grade, reasons=["test"], scorer_version="t")
        if verified_at:
            self.store.set_status(INSTITUTION, asset_id, status="live", verified_via="live", verified_at=verified_at.isoformat())
        return asset_id

    def tick_run(self, index, *, status="succeeded", metrics=None, counts=None, spend=None, gate="passed"):
        """A finished tick, started ``index`` hours after NOW."""

        with mock.patch("app.internet_intelligence.map.store.now_iso", return_value=(NOW + timedelta(hours=index)).isoformat()):
            run_id = self.store.start_map_run(INSTITUTION, kind="tick")
            self.store.finish_map_run(INSTITUTION, run_id, status=status, gate=gate, metrics=metrics, counts=counts, spend=spend)
        return run_id


class RunSeriesTests(Base):
    def test_series_compares_each_tick_with_the_one_before(self):
        record_baseline(self.store, INSTITUTION)
        self.tick_run(1, metrics={"holdout_recall": 0.1, "verified": 10, "precision": 1.0}, counts={"sources": 5, "new_assets": 2, "raised": 1, "leads": 3, "canary_leaks_caught": 0}, spend={"search": 5.0})
        self.tick_run(2, status="failed", counts={"sources": 1}, spend={"fetch": 1.0}, gate=None)
        self.tick_run(3, metrics={"holdout_recall": 0.15, "verified": 14, "precision": 0.9}, counts={"sources": 9, "canary_leaks_caught": 0}, spend={"search": 4.0, "fetch": 6.0})
        self.tick_run(4, metrics={"holdout_recall": 0.15, "verified": 14, "precision": 0.9}, counts={"sources": 2, "canary_leaks_caught": 1}, spend={"fetch": 3.0}, gate="canary_held")
        series = run_series(self.store, INSTITUTION)
        self.assertEqual(len(series), 4, "ticks only: the baseline run is not a tick")
        latest, gained, failed, first = series
        self.assertEqual((gained["holdout_recall_change"], gained["verified_change"], gained["spend"], gained["cost_per_verified"]), (0.05, 4, 10.0, 2.5), "compared with the last tick that stored metrics")
        self.assertEqual(gained["spend_by_budget"], {"search": 4.0, "fetch": 6.0})
        self.assertEqual((failed["holdout_recall"], failed["holdout_recall_change"], failed["status"]), (None, None, "failed"))
        self.assertEqual((first["holdout_recall_change"], first["verified_change"], first["new_assets"], first["leads"]), (None, None, 2, 3), "nothing to compare the first tick with")
        self.assertEqual((latest["holdout_recall_change"], latest["verified_change"], latest["cost_per_verified"]), (0.0, 0, None), "no verified item gained, no cost per item")
        self.assertEqual((latest["canary_leaks_caught"], latest["gate"], latest["precision"]), (1, "canary_held", 0.9))
        # The oldest tick shown is still compared with the one before it.
        shown = run_series(self.store, INSTITUTION, limit=3)
        self.assertEqual([item["started_at"] for item in shown], [item["started_at"] for item in series[:3]])
        self.assertEqual(shown[1]["holdout_recall_change"], 0.05)

    def test_managers_get_the_series_and_cost_readers_do_not(self):
        self.tick_run(1, metrics={"holdout_recall": 0.1, "verified": 1}, counts={"sources": 1}, spend={"search": 1.0})
        source_id, _ = self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|instagram.com", origin="recurring")
        self.store.complete_source(INSTITUTION, source_id, outcome="ok", next_due=NOW.isoformat(), interval_seconds=86400, yield_count=0, cost=6.0, failed=False)
        self.asset("https://www.instagram.com/bgscet_engg_coll/", "A")
        self.asset("https://www.youtube.com/@bgscet", "B")
        service = MapService(self.store)
        manager = service.summary(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION)
        reader = service.summary(principal(PrincipalType.FACULTY, college_id=INSTITUTION), INSTITUTION)
        self.assertEqual(len(manager["series"]), 1)
        self.assertEqual(manager["cost"], {"cost_total": 6.0, "verified": 2, "cost_per_verified": 3.0})
        self.assertEqual(manager["ground_truth"]["precision"], 1.0)
        for key in ("series", "cost", "ground_truth"):
            self.assertNotIn(key, reader)

    def test_the_console_renders_the_series(self):
        js = CONSOLE_JS.read_text(encoding="utf-8")
        self.assertIn("runSeries(data.series || [], data.cost)", js)
        for key in ("holdout_recall_change", "canary_leaks_caught", "cost_per_verified", "within_7_days", "sources_overdue", "entities_total"):
            self.assertIn(key, js)


class PrecisionTests(Base):
    def test_precision_counts_what_readers_are_shown_that_is_known_to_be_wrong(self):
        self.assertIsNone(map_metrics(self.store, INSTITUTION)["precision"], "nothing at B or better, nothing to measure")
        self.asset("https://www.instagram.com/bgscet_engg_coll/", "A")
        self.asset("https://www.youtube.com/@bgscet", "B")
        self.asset("https://www.facebook.com/bgscet.maybe/", "C")
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 1.0)
        # A canary let through at B.
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgs_fake", platform="instagram", split="canary", entity_name="Look-alike", relation="third_party", expected_min_grade="D")
        self.asset("https://www.instagram.com/bgs_fake/", "B")
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 0.6667)
        # An account of a look-alike entity at B.
        lookalike = self.store.upsert_entity(INSTITUTION, name="BGS Group of Institutions", kind="lookalike")
        self.asset("https://www.instagram.com/bgsgroup/", "B", entity_id=lookalike)
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 0.5)
        # A reviewer's rejection counts until a later confirmation overturns it.
        rejected = self.asset("https://x.com/bgscet_rejected", "B")
        self.store.add_evidence(INSTITUTION, asset_id=rejected, kind="reviewer_reject", polarity="refutes", channel="reviewer:r", observed_via="reviewer", observed_at=(NOW - timedelta(days=2)).isoformat())
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 0.4)
        self.store.add_evidence(INSTITUTION, asset_id=rejected, kind="reviewer_confirm", channel="reviewer:r", observed_via="reviewer", observed_at=(NOW - timedelta(days=1)).isoformat())
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 0.6)

    def test_precision_of_a_proposal_uses_the_proposed_grades(self):
        self.asset("https://www.instagram.com/bgscet_engg_coll/", "A")
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgs_fake", platform="instagram", split="canary", entity_name="Look-alike", relation="third_party", expected_min_grade="D")
        fake = self.asset("https://www.instagram.com/bgs_fake/", "C")
        self.store.set_grade(INSTITUTION, fake, grade="B", reasons=["new rule"], scorer_version="t2", proposed=True)
        self.assertEqual(map_metrics(self.store, INSTITUTION)["precision"], 1.0)
        self.assertEqual(map_metrics(self.store, INSTITUTION, proposed=True)["precision"], 0.5)


class SeedVerificationTests(Base):
    def test_an_imported_claim_alone_verifies_nothing(self):
        rows, lookalikes = load_default_seed()
        import_seed(self.store, INSTITUTION, rows, lookalikes=lookalikes)
        regrade(self.store, INSTITUTION)
        metrics = map_metrics(self.store, INSTITUTION)
        self.assertGreater(metrics["seed_total"], 100)
        self.assertEqual((metrics["seed_verified"], metrics["seed_verification_rate"]), (0, 0.0), "the claims grade C, but nothing has re-observed them")
        # A seed the map sees for itself (a search snippet here) is verified at its expected grade.
        seed = next(item for item in self.store.list_gold(INSTITUTION, split="seed") if item["expected_min_grade"] == "C")
        asset = self.store.find_asset(INSTITUTION, seed["asset_key"])
        self.store.add_evidence(INSTITUTION, asset_id=asset["asset_id"], kind="search_snippet", channel="search:test", observed_via="index")
        regrade(self.store, INSTITUTION, [asset["asset_id"]])
        self.assertEqual(map_metrics(self.store, INSTITUTION)["seed_verified"], 1)


class FreshnessTests(Base):
    def test_freshness_of_what_the_map_stands_behind(self):
        for index, days in enumerate((2, 20, 60, 200)):
            self.asset(f"https://www.instagram.com/fresh{index}/", "B", verified_at=NOW - timedelta(days=days))
        self.asset("https://www.instagram.com/snippets_only/", "B")  # B from two indexes: never checked live
        self.asset("https://www.instagram.com/unverified/", "C", verified_at=NOW)
        for target, due in (("late", NOW - timedelta(days=3)), ("recent", NOW - timedelta(hours=12)), ("ahead", NOW + timedelta(days=1)), ("paused", NOW - timedelta(days=9))):
            source_id, _ = self.store.upsert_source(INSTITUTION, connector="lead_page", target=f"https://{target}.example/", origin="lead", work_class="explore", due_at=due.isoformat())
            if target == "paused":
                self.store.set_source_status(INSTITUTION, source_id, "pruned")
        freshness = map_metrics(self.store, INSTITUTION, now=NOW)["freshness"]
        self.assertEqual(freshness, {"verified": 5, "never_verified": 1, "within_7_days": 0.2, "within_30_days": 0.4, "within_90_days": 0.6, "median_age_days": 40.0, "sources_overdue": 1})
        service = MapService(self.store)
        self.assertNotIn("sources_overdue", service.summary(principal(PrincipalType.FACULTY, college_id=INSTITUTION), INSTITUTION)["freshness"], "the engine's backlog is for managers")
        self.assertIn("sources_overdue", service.summary(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION)["freshness"])


class ReachTests(Base):
    def seed_default(self):
        rows, lookalikes = load_default_seed()
        import_seed(self.store, INSTITUTION, rows, lookalikes=lookalikes)
        return [entity for entity in self.store.list_entities(INSTITUTION) if entity["kind"] != "lookalike"]

    def test_every_entity_is_in_the_grid_and_the_own_come_first(self):
        entities = self.seed_default()
        self.assertGreater(len(entities), 100)
        sync_profile(self.store, InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", official_domains=["bgscet.ac.in"]))
        grid = gap_grid(self.store, INSTITUTION, own_groups=["BGSCET"])
        self.assertEqual((len(grid["rows"]), grid["entities_total"], grid["truncated"]), (len(entities), len(entities), False), "no silent cap")
        self.assertEqual(grid["rows"][0]["entity_id"], self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")["entity_id"], "the entity holding the configured domain first")
        labels = {entity["entity_id"]: entity["group_label"] for entity in entities}
        own = [row for row in grid["rows"][1:] if labels[row["entity_id"]] == "BGSCET"]
        self.assertEqual(grid["rows"][1 : 1 + len(own)], own, "then the institution's own sweep group")
        self.assertTrue(any(labels[row["entity_id"]].startswith("Mutt") for row in grid["rows"]), "the Math and its branches are shown")
        shown = gap_grid(self.store, INSTITUTION, max_entities=5)
        self.assertEqual((len(shown["rows"]), shown["entities_total"], shown["truncated"], shown["cells"]), (5, len(entities), True, 5 * 10), "a display cap says so")

    def test_nominated_entities_lead_and_their_groups_stand_in_for_configured_ones(self):
        alpha = self.store.upsert_entity(INSTITUTION, name="Alpha", group_label="Aaa")
        beta = self.store.upsert_entity(INSTITUTION, name="Beta", group_label="Bbb")
        own = self.store.upsert_entity(INSTITUTION, name="Own College", group_label="Zzz")
        unit = self.store.upsert_entity(INSTITUTION, name="Own Unit", kind="unit", group_label="Zzz")
        self.store.upsert_entity(INSTITUTION, name="Own Look-alike", kind="lookalike")
        sync_profile(self.store, InstitutionProfile(INSTITUTION, "Own College", "Bengaluru", official_domains=["own.example"]))
        order = [entity["entity_id"] for entity in ranked_entities(self.store, INSTITUTION)]
        self.assertEqual(order[:2], [own, unit], "the nominated entity, then its group")
        self.assertEqual(set(order[2:]), {alpha, beta, self.entity_id})
        self.assertEqual([entity["entity_id"] for entity in ranked_entities(self.store, INSTITUTION, own_groups=["bbb"])][:2], [own, beta], "configured groups replace the stand-in")

    def test_search_plans_a_source_for_every_entity(self):
        entities = self.seed_default()
        context = ConnectorContext(self.store, INSTITUTION, "planning", NOW, search=NoHits())
        leads = SearchConnector(active=True).plan(context)
        self.assertEqual(len(leads), len(entities) * len(PLATFORM_DOMAINS), "the budget, not a cap, limits what the rotation costs")
        self.assertEqual({lead.entity_id for lead in leads}, {entity["entity_id"] for entity in entities})
        self.assertEqual(leads[0].entity_id, gap_grid(self.store, INSTITUTION)["rows"][0]["entity_id"], "planned in the grid's order")

    def test_a_group_covers_its_cell(self):
        for url in ("https://www.reddit.com/r/bgscet/", "https://t.me/bgscet_official"):
            asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation="official")
            self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="official_link", channel="site:bgscet.ac.in", detail="A:footer", observed_via="live")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.store.find_asset(INSTITUTION, "reddit:r:bgscet")["kind"], "group")
        cells = gap_grid(self.store, INSTITUTION)["rows"][0]["cells"]
        self.assertEqual((cells["reddit"], cells["telegram"]), ({"grade": "A", "covered": True}, {"grade": "A", "covered": True}))
        for domain in PLATFORM_DOMAINS:
            self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|{domain}", origin="recurring", interval_seconds=14 * 86400)
        prioritise_gaps(self.store, INSTITUTION, now=NOW)
        topics = {row["target"].split("|")[1]: row["topic"] for row in self.store.list_sources(INSTITUTION, connector="search")}
        self.assertEqual((topics["reddit.com"], topics["t.me"], topics["youtube.com"]), ("", "", "gap"))


class GapCadenceTests(Base):
    def test_a_flagged_search_is_due_within_the_gap_interval(self):
        later, _ = self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|instagram.com", origin="recurring", interval_seconds=14 * 86400, due_at=(NOW + timedelta(days=14)).isoformat())
        sooner, _ = self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|youtube.com", origin="recurring", interval_seconds=14 * 86400, due_at=(NOW + timedelta(hours=2)).isoformat())
        prioritise_gaps(self.store, INSTITUTION, now=NOW)
        flagged = self.store.get_source(INSTITUTION, later)
        self.assertEqual((flagged["topic"], flagged["interval_seconds"], flagged["due_at"]), ("gap", GAP_INTERVAL_SECONDS, (NOW + timedelta(seconds=GAP_INTERVAL_SECONDS)).isoformat()))
        self.assertEqual(self.store.get_source(INSTITUTION, sooner)["due_at"], (NOW + timedelta(hours=2)).isoformat(), "one due sooner keeps its turn")

    def test_a_gap_search_is_held_at_the_gap_pace(self):
        engine = MapEngine(self.store, ConnectorRegistry([SearchConnector(active=True)]))
        gap = {"connector": "search", "topic": "gap", "interval_seconds": GAP_INTERVAL_SECONDS, "base_interval_seconds": 14 * 86400, "origin": "recurring", "failure_streak": 0}
        self.assertEqual(engine._next_interval(gap, ConnectorResult()), (GAP_INTERVAL_SECONDS, GAP_INTERVAL_SECONDS), "an empty search does not double it")
        self.assertEqual(engine._next_interval({**gap, "failure_streak": 3}, ConnectorResult(failed=True)), (GAP_INTERVAL_SECONDS, GAP_INTERVAL_SECONDS), "nor does a failure")
        self.assertEqual(engine._next_interval({**gap, "topic": ""}, ConnectorResult()), (2 * GAP_INTERVAL_SECONDS, 2 * GAP_INTERVAL_SECONDS), "once covered it drifts back toward its base")

    def test_ticks_come_round_every_gap_interval(self):
        clock = Clock()
        profile = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru")
        engine = MapEngine(self.store, ConnectorRegistry([SearchConnector(active=True)]), EngineConfig(sources_per_tick=50), search=NoHits(), profile_loader=lambda _: profile, clock=clock)

        def youtube():
            return next(row for row in self.store.list_sources(INSTITUTION, connector="search") if row["target"].endswith("|youtube.com"))

        asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual((youtube()["runs"], youtube()["due_at"]), (1, (NOW + timedelta(days=3)).isoformat()))
        clock.at = NOW + timedelta(days=3)
        asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual((youtube()["runs"], youtube()["topic"], youtube()["interval_seconds"], youtube()["due_at"]), (2, "gap", GAP_INTERVAL_SECONDS, (NOW + timedelta(days=6)).isoformat()))

    def test_a_source_from_before_the_base_interval_returns_to_its_connectors_default(self):
        engine = MapEngine(self.store, ConnectorRegistry([SearchConnector(active=True)]))
        legacy = {"connector": "search", "topic": "", "interval_seconds": GAP_INTERVAL_SECONDS, "base_interval_seconds": None, "origin": "recurring", "failure_streak": 0}
        days = []
        for _ in range(3):
            legacy["interval_seconds"] = engine._next_interval(legacy, ConnectorResult())[1]
            days.append(legacy["interval_seconds"] // 86400)
        self.assertEqual(days, [6, 12, 14], "a covered cell's search goes back to fortnightly")
        productive = {**legacy, "interval_seconds": 86400}
        for _ in range(10):
            productive["interval_seconds"] = engine._next_interval(productive, ConnectorResult(yield_count=1))[1]
        self.assertEqual(productive["interval_seconds"], 14 * 86400 // 8, "yield speeds it up only so far, not to the hourly minimum")
        # The rows themselves get the base on the next tick.
        source_id, _ = self.store.upsert_source(INSTITUTION, connector="search", target=f"{self.entity_id}|x.com", origin="recurring", interval_seconds=GAP_INTERVAL_SECONDS)
        with self.store.backend.transaction():
            self.store.backend.execute("UPDATE intel_sources SET base_interval_seconds = NULL WHERE source_id = ?", (source_id,))
        engine.ensure_recurring(INSTITUTION, None)
        self.assertEqual(self.store.get_source(INSTITUTION, source_id)["base_interval_seconds"], 14 * 86400)


class CoverageTests(Base):
    def test_coverage_counts_what_the_two_captures_found_and_never_passes_one(self):
        rows, lookalikes = load_default_seed()
        import_seed(self.store, INSTITUTION, rows, lookalikes=lookalikes)

        def account(url, *kinds):
            asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation="official")
            for kind in kinds:
                live = kind == "official_link"
                self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind=kind, channel="site:x" if live else "search:t", detail="A:footer" if live else "", observed_via="live" if live else "index")

        for name in ("s1", "s2", "s3"):
            account(f"https://www.instagram.com/{name}/", "official_link")
        for name in ("i1", "i2", "i3"):
            account(f"https://www.instagram.com/{name}/", "search_snippet")
        account("https://www.instagram.com/b1/", "official_link", "search_snippet")
        account("https://www.reddit.com/r/bgscet_dashboard_test/", "official_link")  # a group counts like an account
        regrade(self.store, INSTITUTION)
        estimate = coverage_estimate(self.store, INSTITUTION)
        # Site 5, index 4, both 1: Chapman gives 6 * 5 / 2 - 1 = 14, and the two found 8 between them.
        self.assertEqual((estimate["captured"], estimate["estimated_total"]), (8, 14.0))
        self.assertEqual(estimate["coverage"], round(8 / 14, 3))
        self.assertEqual(estimate["other_known"], estimate["known"] - 8, "the imported claims are known, not found by either capture")
        self.assertGreater(estimate["other_known"], 100)
        self.assertLessEqual(estimate["coverage"], 1.0)


class ReaderMetricsTests(Base):
    def test_readers_get_what_the_summary_shows_them(self):
        record_baseline(self.store, INSTITUTION, import_record={"imported_by": "principal-1", "approved_by": "Trust IT office"})
        self.tick_run(1, metrics={"holdout_recall": 0.1}, counts={"sources": 3}, spend={"search": 42.0})
        self.asset("https://www.instagram.com/bgscet_engg_coll/", "A")
        self.asset("https://www.youtube.com/@bgscet", "C")
        service = MapService(self.store)
        reader = service.metrics(principal(PrincipalType.FACULTY, college_id=INSTITUTION), INSTITUTION)
        self.assertEqual(set(reader), {"assets", "verified", "by_platform", "by_grade", "freshness"})
        self.assertEqual(set(reader["by_grade"]), {"O", "A", "A-arch", "B"})
        self.assertNotIn("sources_overdue", reader["freshness"])
        manager = service.metrics(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION, kind="tick", limit=5)
        self.assertEqual([run["kind"] for run in manager["runs"]], ["tick"], "managers filter runs by kind")
        for key in ("holdout_recall", "precision", "canary_leaks", "seed_verification_rate"):
            self.assertIn(key, manager)
        self.assertEqual(len(service.metrics(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION, limit=1)["runs"]), 1)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
    def test_metrics_route(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "met-principal", "X-Demo-Role": "principal", "X-Demo-College": "met_college"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        record_baseline(store, "met_college")
        with mock.patch.object(platform, "intelligence_map", MapService(store)):
            managed = client.get("/v1/intelligence/map/metrics", headers=headers, params={"kind": "baseline", "limit": 1})
            self.assertEqual(managed.status_code, 200, managed.text)
            self.assertEqual([run["kind"] for run in managed.json()["runs"]], ["baseline"])
            read = client.get("/v1/intelligence/map/metrics", headers={**headers, "X-Demo-Principal": "met-faculty", "X-Demo-Role": "faculty"})
            self.assertEqual(read.status_code, 200)
            self.assertNotIn("runs", read.json())
            self.assertNotIn("holdout_recall", read.json())


if __name__ == "__main__":
    unittest.main()
