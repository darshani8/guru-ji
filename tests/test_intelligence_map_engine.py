import importlib.util
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app.internet_intelligence.fetch import PublicPageFetcher
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry, ConnectorResult, Lead
from app.internet_intelligence.map.connectors.web import LeadPageConnector, OfficialSiteConnector, RecheckConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.pipeline import sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.workers.handlers import register_handlers

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
PUBLIC_IP = "93.184.216.34"
DAY = NOW.date().isoformat()

HOME = """<html><head><title>BGS College of Engineering and Technology</title>
<link rel="alternate" type="application/rss+xml" href="/feed/"></head><body>
<header><nav><a href="/contact-us/">Contact</a></nav></header>
<main><p>BGS College of Engineering and Technology, Mahalakshmipuram, Bengaluru.</p>
<a href="https://bgscet-alumni.example/">Alumni association</a> <a href="https://unrelated-shop.example/">Our caterer</a></main>
<footer><a href="https://www.instagram.com/bgscet_engg_coll/">Instagram</a> <a href="https://www.youtube.com/channel/UClACJA8pf4QbkdH4__7MTGA">YouTube</a></footer>
</body></html>"""
CONTACT = "<html><head><title>Contact</title></head><body><main><p>Write to us.</p></main></body></html>"
ALUMNI = """<html><head><title>BGS College of Engineering and Technology Alumni</title></head><body>
<p>The alumni association of BGS College of Engineering and Technology, Bengaluru (BGSCET).</p>
<footer><a href="https://www.instagram.com/bgscet_alumni/">Instagram</a></footer></body></html>"""
SHOP = "<html><head><title>Fresh Bakes</title></head><body><p>Cakes and snacks delivered across the city.</p></body></html>"


def _host(request: httpx.Request) -> str:
    return request.headers.get("host", request.url.host).split(":")[0]


@dataclass
class Site:
    """A fake web: pages by URL, with ETags so conditional requests can be answered 304."""

    pages: dict
    etags: bool = False

    def __post_init__(self):
        self.seen: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = f"https://{_host(request)}{request.url.path}"
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        self.seen.append((url, request.headers.get("if-none-match")))
        status, body, *content_type = self.pages.get(url, (404, ""))
        headers = {"content-type": content_type[0] if content_type else "text/html; charset=utf-8"}
        if self.etags and status == 200:
            tag = f'"{abs(hash(body)) % 10**8}"'
            if request.headers.get("if-none-match") == tag:
                return httpx.Response(304, headers={"etag": tag}, request=request)
            headers["etag"] = tag
        return httpx.Response(status, headers=headers, text=body, request=request)

    def fetcher(self) -> PublicPageFetcher:
        return PublicPageFetcher(transport=httpx.MockTransport(self.handler), resolver=lambda host: (PUBLIC_IP,))


PROFILE = InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])


class Clock:
    def __init__(self, at: datetime = NOW):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at = self.at + timedelta(**delta)


class SourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def test_a_source_is_added_once_and_keeps_its_history(self):
        first, created = self.store.upsert_source("bgscet", connector="lead_page", target="https://a.example/", origin="lead", work_class="explore", hops=2)
        again, created_again = self.store.upsert_source("bgscet", connector="lead_page", target="https://a.example/", origin="lead", work_class="explore", hops=1)
        self.assertEqual((first, created, created_again), (again, True, False))
        self.assertEqual(self.store.get_source("bgscet", first)["hops"], 1, "a shorter path lowers the hop count")
        with self.assertRaises(ValueError):
            self.store.upsert_source("bgscet", connector="x", target="t", origin="guess")
        with self.assertRaises(ValueError):
            self.store.upsert_source("bgscet", connector="x", target="  ")
        self.assertEqual(self.store.list_sources("other"), [], "sources belong to one institution")

    def test_a_lease_is_won_once_until_it_lapses(self):
        now = NOW.isoformat()
        for index in range(3):
            self.store.upsert_source("bgscet", connector="lead_page", target=f"https://s{index}.example/", origin="lead", work_class="explore", due_at=now)
        self.store.upsert_source("bgscet", connector="recheck", target="asset-1", work_class="recheck", due_at=now)
        self.store.upsert_source("bgscet", connector="lead_page", target="https://later.example/", origin="lead", work_class="explore", due_at=(NOW + timedelta(days=1)).isoformat())
        first = self.store.claim_due("bgscet", now=now, worker="w1", lease_seconds=900, limit=10, work_class="explore")
        self.assertEqual(len(first), 3, "only due sources of the class are claimed")
        self.assertEqual(self.store.claim_due("bgscet", now=now, worker="w2", lease_seconds=900, limit=10, work_class="explore"), [], "a leased source is not claimed twice")
        self.assertEqual([row["connector"] for row in self.store.claim_due("bgscet", now=now, worker="w2", lease_seconds=900, limit=10, connectors=["recheck"])], ["recheck"])
        self.assertEqual(self.store.claim_due("bgscet", now=now, worker="w2", lease_seconds=900, limit=10, connectors=[]), [])
        lapsed = (NOW + timedelta(seconds=901)).isoformat()
        self.assertEqual(len(self.store.claim_due("bgscet", now=lapsed, worker="w3", lease_seconds=900, limit=10, work_class="explore")), 3, "a lapsed lease is free again")
        source = first[0]
        self.store.complete_source("bgscet", source["source_id"], outcome="timeout", next_due=lapsed, interval_seconds=10, yield_count=0, cost=1, failed=True)
        row = self.store.get_source("bgscet", source["source_id"])
        self.assertEqual((row["failure_streak"], row["runs"], row["lease_until"], row["interval_seconds"]), (1, 1, None, 60))
        self.store.complete_source("bgscet", source["source_id"], outcome="ok", next_due=lapsed, interval_seconds=3600, yield_count=2, cost=1, failed=False, status="pruned")
        row = self.store.get_source("bgscet", source["source_id"])
        self.assertEqual((row["failure_streak"], row["yield_total"], row["status"], row["cost_total"]), (0, 2, "pruned", 2.0))

    def test_leads_that_never_produced_expire(self):
        past, now = (NOW - timedelta(days=1)).isoformat(), NOW.isoformat()
        dud, _ = self.store.upsert_source("bgscet", connector="lead_page", target="https://dud.example/", origin="lead", work_class="explore", expires_at=past)
        productive, _ = self.store.upsert_source("bgscet", connector="lead_page", target="https://good.example/", origin="lead", work_class="explore", expires_at=past)
        waiting, _ = self.store.upsert_source("bgscet", connector="lead_page", target="https://deferred.example/", origin="lead", work_class="explore", expires_at=past)
        watch, _ = self.store.upsert_source("bgscet", connector="official_site", target="asset-1", origin="recurring")
        self.store.complete_source("bgscet", productive, outcome="ok", next_due=now, interval_seconds=3600, yield_count=1, cost=1, failed=False)
        self.store.complete_source("bgscet", dud, outcome="ok", next_due=now, interval_seconds=3600, yield_count=0, cost=1, failed=False)
        self.assertEqual(self.store.expire_sources("bgscet", now=now), 1)
        statuses = {source_id: self.store.get_source("bgscet", source_id)["status"] for source_id in (dud, productive, waiting, watch)}
        self.assertEqual(statuses, {dud: "expired", productive: "active", waiting: "active", watch: "active"}, "a lead that never got to run (a spent budget) is not judged yet")

    def test_budgets_hold_per_institution_and_platform_wide(self):
        reserve = self.store.reserve_budget
        self.assertTrue(reserve("a", connector="fetch", units=4, day=DAY, global_cap=10, tenant_cap=5))
        self.assertFalse(reserve("a", connector="fetch", units=2, day=DAY, global_cap=10, tenant_cap=5), "the tenant quota is spent")
        self.assertTrue(reserve("b", connector="fetch", units=5, day=DAY, global_cap=10, tenant_cap=5))
        self.assertFalse(reserve("c", connector="fetch", units=2, day=DAY, global_cap=10, tenant_cap=5), "the platform cap is spent")
        self.assertEqual(self.store.tenant_spend("c", day=DAY).get("fetch", {"units": 0.0})["units"], 0.0, "a refused reservation hands the tenant quota back")
        self.assertFalse(reserve("d", connector="fetch", units=6, day=DAY, global_cap=10, tenant_cap=5), "one run larger than the quota never fits")
        self.assertTrue(reserve("a", connector="fetch", units=0, day=DAY, global_cap=0, tenant_cap=0), "free work needs no budget")
        self.assertEqual(self.store.spend(day=DAY)["fetch"], {"units": 9.0, "calls": 2})
        self.assertTrue(reserve("a", connector="fetch", units=4, day="2026-09-23", global_cap=10, tenant_cap=5), "budgets reset daily")

    def test_the_fetch_cache_and_the_run_lock(self):
        self.assertIsNone(self.store.fetch_state("bgscet", "https://bgscet.ac.in/"))
        self.store.record_fetch("bgscet", "https://bgscet.ac.in/", outcome="ok", etag='"v1"', last_modified=None, content_sha256="abc")
        self.store.record_fetch("bgscet", "https://bgscet.ac.in/", outcome="not_modified", etag='"v1"', last_modified="Mon", content_sha256=None)
        state = self.store.fetch_state("bgscet", "https://bgscet.ac.in/")
        self.assertEqual((state["etag"], state["last_modified"], state["outcome"], state["content_sha256"]), ('"v1"', "Mon", "not_modified", "abc"))
        self.assertIsNone(self.store.fetch_state("other", "https://bgscet.ac.in/"), "validators belong to the institution that read the page")
        run = self.store.try_start_map_run("bgscet", kind="tick", lock_seconds=3600)
        self.assertIsNotNone(run)
        self.assertIsNone(self.store.try_start_map_run("bgscet", kind="tick", lock_seconds=3600), "one tick at a time per institution")
        self.assertIsNotNone(self.store.try_start_map_run("other", kind="tick", lock_seconds=3600))
        self.assertIsNotNone(self.store.try_start_map_run("bgscet", kind="tick", lock_seconds=0), "a run silent past the lock window is abandoned")
        self.assertEqual({row["status"] for row in self.store.list_map_runs("bgscet")}, {"abandoned", "running"})


class _Exploding:
    name = "exploding"
    access_mode = "public_page"
    budget_key = "fetch"
    max_grade = "C"
    default_interval = 86400

    def enabled(self):
        return True

    def cost(self, source):
        return 1.0

    async def run(self, source, context):
        raise RuntimeError("boom")


class _Planner(_Exploding):
    name = "planner"

    def plan(self, context: ConnectorContext):
        return [Lead("planner", "https://planned.example/", work_class="rotation", origin="gap", hops=0), Lead("planner", "https://too-far.example/", hops=9)]

    async def run(self, source, context):
        return ConnectorResult(outcome="ok", yield_count=1)


class _Off(_Exploding):
    name = "off"

    def enabled(self):
        return False


class RegistryTests(unittest.TestCase):
    def test_prohibited_and_unknown_access_modes_are_refused(self):
        for mode in ("logged_in_session", "credential", "proxy_rotation", "captcha_solving", "ua_spoofing", "something_new"):
            bad = type("Bad", (_Exploding,), {"access_mode": mode, "name": mode})()
            with self.assertRaises(ValueError):
                ConnectorRegistry([bad])
        registry = ConnectorRegistry([_Exploding(), _Off()])
        self.assertEqual(registry.names(), ["exploding"])
        self.assertIsNone(registry.get("off"), "a disabled connector is never handed out")
        self.assertEqual({item["name"]: item["enabled"] for item in registry.describe()}, {"exploding": True, "off": False})


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.clock = Clock()
        self.site = Site({
            "https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT),
            "https://bgscet-alumni.example/": (200, ALUMNI), "https://unrelated-shop.example/": (200, SHOP),
        }, etags=True)

    def engine(self, *connectors, **config) -> MapEngine:
        registry = ConnectorRegistry(connectors or (OfficialSiteConnector(), LeadPageConnector(), RecheckConnector()))
        return MapEngine(self.store, registry, EngineConfig(**config), fetcher=self.site.fetcher(), profile_loader=lambda institution_id: PROFILE if institution_id == "bgscet" else None, clock=self.clock)

    async def test_ticks_grow_the_map_from_the_official_site(self):
        engine = self.engine()
        first = await engine.tick("bgscet")
        self.assertEqual(first["stop_reason"], "completed")
        self.assertEqual(first["counts"]["sources"], 1, "the configured domain is watched from the first tick")
        grades = {asset["asset_key"]: asset["grade"] for asset in self.store.list_assets("bgscet")}
        self.assertEqual(grades["instagram:bgscet_engg_coll"], "A")
        self.assertEqual(grades["youtube:channel:UClACJA8pf4QbkdH4__7MTGA"], "A")
        sources = {(row["connector"], row["target"]): row for row in self.store.list_sources("bgscet")}
        lead = sources[("lead_page", "https://bgscet-alumni.example/")]
        self.assertEqual((lead["origin"], lead["work_class"], lead["hops"]), ("lead", "explore", 1))
        self.assertIsNotNone(lead["expires_at"], "a lead expires unless it produces something")
        self.assertIn(("feed", "https://bgscet.ac.in/feed/"), sources, "feeds found on the site become watches for the feed connector")
        watch = sources[("official_site", self.store.list_assets("bgscet", kind="domain")[0]["asset_id"])]
        self.assertEqual(watch["origin"], "recurring")
        self.assertGreater(watch["due_at"], NOW.isoformat(), "the watch is rescheduled")
        self.assertEqual(first["spend"], {"fetch": 2.0}, "charged the two requests the harvest made, not the five it reserved")
        self.assertEqual(self.store.tenant_spend("bgscet", day=NOW.date().isoformat())["fetch"]["units"], 2.0, "the rest of the reservation was handed back")
        self.assertEqual(self.store.list_map_runs("bgscet", kind="tick")[0]["status"], "succeeded")

        # The next tick follows the leads: the alumni site names the college, the shop does not.
        self.clock.advance(minutes=5)
        second = await engine.tick("bgscet")
        self.assertEqual(second["counts"]["sources"], 2)
        alumni = self.store.find_asset("bgscet", "web:bgscet-alumni.example")
        self.assertEqual((alumni["grade"], alumni["relation"]), ("C", "unknown"), "a lead never becomes official on its own")
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_alumni")["grade"], "C", "what a C site links is C")
        self.assertIsNone(self.store.find_asset("bgscet", "web:unrelated-shop.example"))
        sources = {row["target"]: row for row in self.store.list_sources("bgscet")}
        self.assertEqual(sources["https://unrelated-shop.example/"]["status"], "pruned")
        self.assertEqual((sources["https://bgscet-alumni.example/"]["origin"], sources["https://bgscet-alumni.example/"]["expires_at"]), ("recurring", None), "a productive lead is promoted")

        # Nothing is due now: the tick is idle and spends nothing.
        self.clock.advance(minutes=5)
        idle = await engine.tick("bgscet")
        self.assertEqual((idle["stop_reason"], idle["spend"]), ("idle", {}))

    async def test_an_unchanged_site_is_reconfirmed_from_a_304(self):
        engine = self.engine(OfficialSiteConnector())
        await engine.tick("bgscet")
        self.clock.advance(days=8)
        before = len(self.store.list_evidence("bgscet", limit=10000))
        again = await engine.tick("bgscet")
        self.assertEqual(again["counts"]["sources"], 1)
        homepage_requests = [tag for url, tag in self.site.seen if url == "https://bgscet.ac.in/"]
        self.assertIsNotNone(homepage_requests[-1], "the second fetch is conditional")
        reconfirmed = [item for item in self.store.list_evidence("bgscet", limit=10000)[before:] if item["kind"] == "official_link"]
        self.assertEqual(len(reconfirmed), 2, "each account the unchanged homepage linked is observed again")
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "A")

    async def test_work_that_does_not_fit_the_budget_is_deferred_not_dropped(self):
        engine = self.engine(budgets={"fetch": 6}, tenant_share=0.5)
        result = await engine.tick("bgscet")
        self.assertEqual((result["stop_reason"], result["counts"]["deferred_budget"], result["counts"]["sources"]), ("budget", 1, 0))
        watch = [row for row in self.store.list_sources("bgscet") if row["connector"] == "official_site"][0]
        self.assertEqual((watch["lease_until"], watch["runs"], watch["status"]), (None, 0, "active"), "the deferred source is released for a later tick")

    async def test_a_broken_connector_fails_its_source_not_the_tick(self):
        self.store.upsert_source("bgscet", connector="exploding", target="t", origin="seed", due_at=NOW.isoformat())
        engine = self.engine(_Exploding())
        result = await engine.tick("bgscet")
        self.assertEqual((result["counts"]["failed"], result["stop_reason"]), (1, "dry"))
        source = self.store.list_sources("bgscet", connector="exploding")[0]
        self.assertEqual((source["failure_streak"], source["last_outcome"]), (1, "error:RuntimeError"))
        self.assertEqual(source["due_at"], (NOW + timedelta(seconds=86400 * 2)).isoformat(), "a failing source backs off")

    async def test_planned_leads_hop_cap_suppression_and_the_run_lock(self):
        self.store.suppress("bgscet", "https://hidden.example/", reason="asked to be left out")
        engine = self.engine(_Planner(), _Off())
        self.assertFalse(engine._add_lead("bgscet", Lead("planner", "https://hidden.example/")))
        self.assertFalse(engine._add_lead("bgscet", Lead("planner", "https://deep.example/", hops=3)))
        self.store.upsert_source("bgscet", connector="off", target="never", due_at=NOW.isoformat())
        result = await engine.tick("bgscet")
        targets = {row["target"]: row for row in self.store.list_sources("bgscet")}
        self.assertIn("https://planned.example/", targets)
        self.assertIsNone(targets["https://planned.example/"]["expires_at"], "planned (gap) sources do not expire")
        self.assertNotIn("https://too-far.example/", targets)
        self.assertEqual(targets["never"]["runs"], 0, "a disabled connector's sources wait")
        self.assertEqual(result["counts"]["sources"], 1)
        self.store.try_start_map_run("bgscet", kind="tick", lock_seconds=3600)
        self.assertIn("skipped", await engine.tick("bgscet"))

    def test_intervals_adapt_to_yield_and_recover_from_failures(self):
        engine = self.engine()
        base = {"interval_seconds": 86400, "base_interval_seconds": 86400, "failure_streak": 0, "origin": "lead"}
        self.assertEqual(engine._next_interval(base, ConnectorResult(yield_count=2)), (43200, 43200))
        self.assertEqual(engine._next_interval(base, ConnectorResult()), (172800, 172800))
        self.assertEqual(engine._next_interval({**base, "origin": "recurring"}, ConnectorResult()), (86400, 86400))
        self.assertEqual(engine._next_interval({**base, "failure_streak": 9}, ConnectorResult(failed=True)), (30 * 86400, 86400), "back-off delays the next run only, and is capped")
        self.assertEqual(engine._next_interval({**base, "interval_seconds": 60, "base_interval_seconds": 60}, ConnectorResult(yield_count=1)), (3600, 3600), "never more often than hourly")
        # A weekly watch that failed twice and then answers is weekly again.
        week = {"interval_seconds": 7 * 86400, "base_interval_seconds": 7 * 86400, "failure_streak": 0, "origin": "recurring"}
        self.assertEqual(engine._next_interval(week, ConnectorResult(failed=True))[1], 7 * 86400)
        self.assertEqual(engine._next_interval({**week, "failure_streak": 1}, ConnectorResult(failed=True)), (28 * 86400, 7 * 86400))
        self.assertEqual(engine._next_interval({**week, "interval_seconds": 30 * 86400, "failure_streak": 2}, ConnectorResult()), (7 * 86400, 7 * 86400), "an interval stretched by an old outage comes back to base")
        self.assertEqual(engine._next_interval({**week, "interval_seconds": 86400}, ConnectorResult()), (2 * 86400, 2 * 86400), "an idle watch drifts back toward base")
        productive = {"interval_seconds": 14 * 86400, "base_interval_seconds": 14 * 86400, "failure_streak": 0, "origin": "recurring"}
        for _ in range(10):
            productive["interval_seconds"] = engine._next_interval(productive, ConnectorResult(yield_count=3))[1]
        self.assertEqual(productive["interval_seconds"], 14 * 86400 // 8, "yield speeds a source up, but only so far")

    async def test_the_job_handler_and_tick_all(self):
        engine = self.engine()

        class Queue:
            def __init__(self):
                self.handlers = {}

            def register(self, name, handler):
                self.handlers[name] = handler

        queue = Queue()
        register_handlers(queue, map_engine=engine)
        result = await queue.handlers["intelligence.map_tick"]({"institution_id": "bgscet", "_attempt": 1})
        self.assertEqual(result["counts"]["sources"], 1)
        results = await engine.tick_all(["bgscet", "nobody"])
        self.assertEqual([item.get("stop_reason") for item in results], ["completed", "idle"], "the second tick follows the leads the first one found")


class RecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_recheck_records_liveness_and_leaves_social_platforms_alone(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        site = Site({"https://bgscet.ac.in/": (200, HOME)})
        from app.internet_intelligence.map.assets import asset_ref

        page_id, _ = store.upsert_asset("bgscet", asset_ref("https://bgscet.ac.in/events/fest"), entity_id=None)
        social_id, _ = store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/p/abc/"), entity_id=None)
        context = ConnectorContext(store, "bgscet", "run-1", NOW, fetcher=site.fetcher())
        dead = await RecheckConnector().run({"target": page_id}, context)
        self.assertEqual((dead.outcome, dead.failed), ("not_found", True))
        self.assertEqual(store.list_evidence("bgscet", asset_id=page_id)[-1]["polarity"], "refutes")
        social = await RecheckConnector().run({"target": social_id}, context)
        self.assertEqual((social.outcome, social.failed), ("snippet_only", False))
        self.assertEqual((await RecheckConnector().run({"target": "missing"}, context)).prune, True)

    async def test_the_harvester_cache_is_off_unless_asked(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        domain_id = sync_profile(store, PROFILE)["domains_added"][0]
        site = Site({"https://bgscet.ac.in/": (200, HOME)}, etags=True)
        await OfficialSiteHarvester(site.fetcher(), store).harvest("bgscet", domain_id)
        self.assertIsNone(store.fetch_state("bgscet", "https://bgscet.ac.in/"), "a manual harvest does not touch the fetch cache")


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class EngineRouteTests(unittest.TestCase):
    def test_tick_sources_connectors_and_spend(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "engine-principal", "X-Demo-Role": "principal", "X-Demo-College": "engine_college"}
        faculty = {**headers, "X-Demo-Principal": "engine-faculty", "X-Demo-Role": "faculty"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        engine = MapEngine(store, ConnectorRegistry([OfficialSiteConnector(), LeadPageConnector(), RecheckConnector()]), fetcher=None)
        with mock.patch.object(platform, "intelligence_map", MapService(store, engine=engine)):
            ticked = client.post("/v1/intelligence/map/tick?background=false", headers=headers, json={})
            self.assertEqual(ticked.status_code, 200, ticked.text)
            self.assertEqual(ticked.json()["stop_reason"], "idle")
            self.assertEqual(client.post("/v1/intelligence/map/tick", headers=headers, json={}).status_code, 422, "no worker handles map ticks while the map is off")
            self.assertEqual(client.post("/v1/intelligence/map/tick?background=false", headers=faculty, json={}).status_code, 403)
            store.upsert_source("engine_college", connector="lead_page", target="https://a.example/", origin="lead", work_class="explore")
            listed = client.get("/v1/intelligence/map/sources", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["sources"][0]["target"], "https://a.example/")
            self.assertEqual(client.get("/v1/intelligence/map/sources", headers=faculty).status_code, 403)
            described = client.get("/v1/intelligence/map/connectors", headers=faculty)
            self.assertEqual(described.status_code, 200)
            self.assertEqual({item["access_mode"] for item in described.json()["connectors"]}, {"public_page"})
            spent = client.get("/v1/intelligence/map/spend", headers=headers)
            self.assertEqual(spent.status_code, 200)
            self.assertEqual(spent.json()["caps"]["fetch"], 3000)
            self.assertEqual(spent.json()["tenant_caps"]["fetch"], 1500)
            self.assertIn("intelligence.map.tick", {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)})


if __name__ == "__main__":
    unittest.main()
