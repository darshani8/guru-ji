import asyncio
import unittest
from datetime import datetime, timezone

from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.connectors.search import OPEN_WEB, PLATFORM_DOMAINS, SEARCH_TOPICS, VERIFY_PREFIX, SearchConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.search import SearchHit, StaticSearchProvider

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
INSTITUTION = "bgscet"
PROFILE = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])


class Recording(StaticSearchProvider):
    """A static index that also remembers the domains each search was limited to."""

    def __init__(self, hits):
        super().__init__(hits)
        self.domains = []

    async def search(self, query, *, max_results, days=None, topic="general", include_domains=()):
        self.domains.append(tuple(include_domains))
        return await super().search(query, max_results=max_results, days=days, topic=topic, include_domains=include_domains)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, PROFILE)["entity_id"]
        regrade(self.store, INSTITUTION)

    def context(self, search):
        return ConnectorContext(self.store, INSTITUTION, "run-1", NOW, search=search)


class RotationTests(Base):
    async def test_names_and_topics_come_round_in_turn(self):
        provider = Recording(())
        connector = SearchConnector(active=True)
        queries = []
        for runs in range(2 * len(SEARCH_TOPICS)):
            await connector.run({"target": f"{self.entity_id}|instagram.com", "runs": runs}, self.context(provider))
            queries.append(provider.calls[-1])
        self.assertIn('"BGS College of Engineering and Technology" Bengaluru', queries)
        self.assertIn('"BGSCET" alumni Bengaluru', queries, "a unit's account is found under the short name and a topic")
        self.assertEqual({domains for domains in provider.domains}, {("instagram.com",)})

    async def test_the_open_web_search_turns_sites_into_leads(self):
        site = SearchHit(url="https://bgscet-alumni.example/about", title="BGS College of Engineering and Technology Alumni Association", snippet="Alumni of BGS College of Engineering and Technology, Bengaluru")
        provider = Recording((site,))
        result = await SearchConnector(active=True).run({"target": f"{self.entity_id}|{OPEN_WEB}", "runs": 0}, self.context(provider))
        self.assertEqual(provider.domains, [()], "not limited to any platform")
        self.assertEqual([(lead.connector, lead.target) for lead in result.leads], [("lead_page", "https://bgscet-alumni.example/")])

    async def test_known_accounts_are_re_verified_in_the_index_and_a_miss_takes_nothing(self):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="search_snippet", channel="search:static", detail="static: BGSCET", observed_via="index")
        regrade(self.store, INSTITUTION, [asset_id])
        connector = SearchConnector(active=True)
        planned = [lead for lead in connector.plan(self.context(Recording(()))) if lead.work_class == "recheck"]
        self.assertEqual([lead.target for lead in planned], [f"{VERIFY_PREFIX}{asset_id}"])
        found = Recording((SearchHit(url="https://www.instagram.com/bgscet_engg_coll/?hl=en", title="BGS College of Engineering and Technology (@bgscet_engg_coll)", snippet=""),))
        result = await connector.run({"target": planned[0].target}, self.context(found))
        self.assertEqual((result.outcome, found.calls, found.domains), ("reverified", ['"bgscet_engg_coll"'], [("instagram.com",)]))
        self.assertIn("re-verified", self.store.list_evidence(INSTITUTION, asset_id=asset_id)[-1]["detail"])
        before = len(self.store.list_evidence(INSTITUTION, asset_id=asset_id))
        missed = await connector.run({"target": planned[0].target}, self.context(Recording(())))
        self.assertEqual((missed.outcome, len(self.store.list_evidence(INSTITUTION, asset_id=asset_id))), ("not_in_index", before), "an index is not the platform")


class ClassShareTests(Base):
    def test_each_kind_of_search_spends_only_its_share(self):
        class Searcher:
            name, access_mode, budget_key, max_grade, default_interval = "searcher", "search_index", "search", "C", 86400

            def __init__(self):
                self.classes = []

            def enabled(self):
                return True

            def cost(self, source):
                return 1.0

            async def run(self, source, context):
                self.classes.append(source["work_class"])
                return ConnectorResult(outcome="ok")

        searcher = Searcher()
        for index in range(10):
            for work_class in ("rotation", "explore"):
                self.store.upsert_source(INSTITUTION, connector="searcher", target=f"{work_class}-{index}", work_class=work_class, due_at=NOW.isoformat())
        engine = MapEngine(self.store, ConnectorRegistry([searcher]), EngineConfig(sources_per_tick=20, budgets={"search": 20}, tenant_share=1.0, class_split=(("explore", 1.0),)), clock=lambda: NOW)
        asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual(searcher.classes.count("explore"), 4, "new-lead searches stop at their 20% of the day's 20")
        self.assertEqual(searcher.classes.count("rotation"), 10, "while the rotation goes on within its 60%")
        spent = self.store.tenant_spend(INSTITUTION, day=NOW.date().isoformat())
        self.assertEqual((spent["search:explore"]["units"], spent["search:rotation"]["units"], spent["search"]["units"]), (4.0, 10.0, 14.0))


class PlanTests(Base):
    def test_every_entity_gets_platform_searches_and_one_open_web_search(self):
        leads = SearchConnector(active=True).plan(self.context(Recording(())))
        rotation = [lead for lead in leads if lead.work_class == "rotation"]
        self.assertEqual(len(rotation), len(PLATFORM_DOMAINS))
        self.assertEqual([lead.target for lead in leads if lead.work_class == "explore"], [f"{self.entity_id}|{OPEN_WEB}"])


if __name__ == "__main__":
    unittest.main()
