import asyncio
import json
import unittest
from datetime import timedelta

import httpx

from app.config.settings import AppSettings
from app.internet_intelligence.map.cache import CachingSearchProvider, cached_search, search_cache_key
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry
from app.internet_intelligence.map.connectors.common import ApiClient, api_cache_key
from app.internet_intelligence.map.connectors.search import PLATFORM_DOMAINS, SearchConnector, SpamProbeConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.incidents import IncidentDesk
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.store import GLOBAL_TABLES, TENANT_TABLES, MapStore, render_sql_migration
from app.internet_intelligence.search import SearchHit, StaticSearchProvider
from app.workers.handlers import register_handlers
from test_intelligence_map_engine import NOW, PROFILE, Clock

MATH = "Sri Adichunchanagiri Mahasamsthana Math"
MATH_HIT = SearchHit(url="https://www.instagram.com/adichunchanagiri_math/", title=f"{MATH} (@adichunchanagiri_math) • Instagram", snippet=f"{MATH}, Nagamangala, Karnataka")


class SharedSearchCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    async def test_a_second_institutions_identical_search_costs_nothing(self):
        # Two institutions under the same parent body map the same Math.
        for institution in ("bgscet", "bgsit"):
            self.store.upsert_entity(institution, name=MATH, kind="organisation", locations=["Nagamangala"])
        provider = StaticSearchProvider((MATH_HIT,))
        engine = MapEngine(self.store, ConnectorRegistry([SearchConnector(active=True)]), EngineConfig(sources_per_tick=len(PLATFORM_DOMAINS) + 1), search=provider, clock=Clock(), profile_loader=lambda institution: None)
        first, second = await engine.tick_all(["bgscet", "bgsit"])
        self.assertEqual((first["counts"]["sources"], second["counts"]["sources"]), (len(PLATFORM_DOMAINS) + 1, len(PLATFORM_DOMAINS) + 1))
        self.assertEqual(len(provider.calls), len(PLATFORM_DOMAINS) + 1, "each question (every platform, and the open web) is paid for once, not once per institution")
        costs = {institution: {row["connector"]: row["cost"] for row in self.store.source_yields(institution)} for institution in ("bgscet", "bgsit")}
        self.assertEqual(costs, {"bgscet": {"search": float(len(PLATFORM_DOMAINS) + 1)}, "bgsit": {"search": 0.0}}, "an answer from the shared cache costs 0")
        for institution in ("bgscet", "bgsit"):
            self.assertIsNotNone(self.store.find_asset(institution, "instagram:adichunchanagiri_math"), "the cached answer is as good as a fresh one")
        # A later run, from anywhere, is still answered from the cache.
        wrapper = cached_search(provider, self.store, clock=Clock())
        await wrapper.search(f'"{MATH}" Nagamangala', max_results=10, include_domains=["instagram.com"])
        self.assertEqual((wrapper.provider_calls, len(provider.calls)), (0, len(PLATFORM_DOMAINS) + 1))

    async def test_the_key_is_the_provider_the_normalised_query_and_the_parameters(self):
        base = search_cache_key("tavily", '"BGS  College" Bengaluru', {"max_results": 10, "include_domains": ["x.com", "instagram.com"]})
        self.assertEqual(base, search_cache_key("tavily", '"bgs college"   bengaluru ', {"include_domains": ("instagram.com", "x.com"), "max_results": 10}))
        self.assertNotEqual(base, search_cache_key("static", '"BGS College" Bengaluru', {"max_results": 10, "include_domains": ["x.com", "instagram.com"]}))
        self.assertNotEqual(base, search_cache_key("tavily", '"BGS College" Bengaluru', {"max_results": 5, "include_domains": ["x.com", "instagram.com"]}))
        self.assertNotEqual(base, search_cache_key("tavily", '"BGS College" Bengaluru', {"max_results": 10, "include_domains": ["x.com"]}))
        # The query itself is never stored, only a hash of the key; the payload is the public answer.
        await CachingSearchProvider(StaticSearchProvider((MATH_HIT,)), self.store).search(f'"{MATH}" Nagamangala', max_results=10)
        [row] = self.store.backend.fetchall("SELECT * FROM intel_shared_cache")
        self.assertEqual((len(row["key_sha256"]), row["kind"], json.loads(row["payload"])[0]["url"]), (64, "search", MATH_HIT.url))
        self.assertNotIn("Nagamangala", json.dumps({name: value for name, value in row.items() if name != "payload"}))

    async def test_cached_answers_expire(self):
        start = NOW.isoformat()
        self.store.cache_put("search", "k", [{"url": "https://example.org/"}], 60, now=start)
        self.assertEqual(self.store.cache_get("search", "k", now=(NOW + timedelta(seconds=30)).isoformat()), [{"url": "https://example.org/"}])
        self.assertIsNone(self.store.cache_get("api", "k", now=start), "kinds do not share keys")
        later = (NOW + timedelta(seconds=61)).isoformat()
        self.assertIsNone(self.store.cache_get("search", "k", now=later))
        self.assertEqual(self.store.cache_prune(later), 1)
        self.assertEqual(self.store.backend.fetchall("SELECT * FROM intel_shared_cache"), [])
        # Through the wrapper: after seven days the provider is asked again.
        clock, provider = Clock(), StaticSearchProvider((MATH_HIT,))
        for step in (0, 6, 2):
            clock.advance(days=step)
            hits = await cached_search(provider, self.store, clock=clock).search(f'"{MATH}"', max_results=10)
            self.assertEqual([hit.url for hit in hits], [MATH_HIT.url])
        self.assertEqual(len(provider.calls), 2, "fresh on day 0, cached on day 6, asked again on day 8")

    async def test_the_spam_probe_pays_only_for_calls_it_makes(self):
        synced = sync_profile(self.store, PROFILE)
        domain_id = synced["domains_added"][0]
        regrade(self.store, "bgscet", [domain_id])
        provider = StaticSearchProvider((SearchHit(url="https://bgscet.ac.in/slot-gacor.html", title="Slot gacor", snippet="slot online"),))
        costs = []
        for _ in range(2):
            context = ConnectorContext(self.store, "bgscet", "run-1", NOW, search=cached_search(provider, self.store))
            costs.append((await SpamProbeConnector(active=True).run({"target": domain_id}, context)).cost)
        self.assertEqual(costs, [1.0, 0.0])
        bare = ConnectorContext(self.store, "bgscet", "run-1", NOW, search=provider)
        self.assertEqual((await SpamProbeConnector(active=True).run({"target": domain_id}, bare)).cost, 1.0, "a provider without the cache is one call")
        self.assertEqual((await SearchConnector(active=True).run({"target": "missing|x.com"}, bare)).cost, 0.0, "no call, no cost")


def _api(answers):
    """A fake API (the requests it saw, its handler); ``answers[path]`` is (status, body, content type)."""

    seen = []

    def handler(request):
        seen.append(request)
        status, body, content_type = answers.get(request.url.path, (404, "", "text/plain"))
        return httpx.Response(status, content=body if isinstance(body, str) else json.dumps(body), headers={"content-type": content_type}, request=request)

    return seen, handler


class ApiCacheTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def test_api_answers_are_shared_without_keys_or_tokens(self):
        seen, handler = _api({"/v1/search": (200, {"hits": [1]}, "application/json; charset=utf-8"), "/rdap": (200, "<rdap/>", "application/xml"), "/page": (200, "<html></html>", "text/html"), "/down": (503, "", "text/plain")})
        client = ApiClient(transport=httpx.MockTransport(handler), cache=self.store)
        first = asyncio.run(client.request("https://api.example/v1/search?token=URLTOKEN", params={"q": "bgscet", "key": "SECRETKEY1", "api_key": "SECRETKEY2"}))
        again = asyncio.run(client.request("https://api.example/v1/search?token=OTHER", params={"key": "ANOTHERKEY", "q": "bgscet"}))
        self.assertEqual((first.json(), again.json(), len(seen)), ({"hits": [1]}, {"hits": [1]}, 1), "the same public answer, whoever's key asked")
        self.assertEqual(asyncio.run(client.request("https://api.example/v1/search", params={"q": "other"})).outcome, "ok")
        self.assertEqual(len(seen), 2, "other parameters are another question")
        asyncio.run(client.request("https://api.example/rdap"))
        asyncio.run(client.request("https://api.example/rdap"))
        self.assertEqual(len(seen), 3, "an XML answer is shared too")
        for _ in range(2):
            asyncio.run(client.request("https://api.example/page"))
            asyncio.run(client.request("https://api.example/down"))
            asyncio.run(client.request("https://api.example/v1/search", method="POST", data={"q": "bgscet"}))
            asyncio.run(client.request("https://api.example/v1/search", params={"q": "bgscet"}, headers={"Authorization": "Token SECRETAUTH"}))
        self.assertEqual(len(seen), 11, "HTML, errors, POSTs and requests carrying a credential are never shared")
        stored = json.dumps(self.store.backend.fetchall("SELECT * FROM intel_shared_cache"))
        for secret in ("URLTOKEN", "OTHER", "SECRETKEY", "ANOTHERKEY", "SECRETAUTH"):
            self.assertNotIn(secret, stored)
        key = api_cache_key("https://api.example/v1/search?token=URLTOKEN&a=1", {"q": "bgscet", "KEY": "k1", "api_key": "k2"})
        self.assertEqual(json.loads(key), ["https://api.example/v1/search", [["a", "1"], ["q", "bgscet"]]])
        self.assertIsNone(api_cache_key("https://api.example/x", headers={"X-Api-Key": "k"}))
        self.assertIsNone(api_cache_key("https://user:pass@api.example/x"))
        self.assertIsNone(ApiClient(transport=httpx.MockTransport(handler)).cache, "no cache unless one is given")

    def test_only_open_registries_share_answers(self):
        from app.api.platform_runtime import map_connectors

        connectors = map_connectors(AppSettings(), cache=self.store)._connectors
        shared = sorted(name for name, connector in connectors.items() if getattr(getattr(connector, "client", None), "cache", None) is self.store)
        self.assertEqual(shared, ["certificates", "openstreetmap", "rdap", "wikidata"], "never YouTube (quota per key) or court records (sensitive)")
        self.assertIsNone(connectors["youtube"].client.cache)
        self.assertIsNone(connectors["court_records"].client.cache)
        self.assertIsNone(map_connectors(AppSettings())._connectors["wikidata"].client.cache, "no map store, no cache")


class SharedCacheTableTests(unittest.TestCase):
    def test_the_shared_cache_is_global_and_outside_row_level_security(self):
        self.assertIn("intel_shared_cache", GLOBAL_TABLES)
        self.assertNotIn("intel_shared_cache", TENANT_TABLES)
        sql = render_sql_migration()
        self.assertIn("CREATE TABLE IF NOT EXISTS intel_shared_cache ( key_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL );", sql)
        rls = sql.split("-- PostgreSQL row-level security", 1)[1]
        self.assertNotIn("intel_shared_cache", rls, "no RLS and no tenant policy: every institution reads the same public answers")
        # Readable from any tenant's transaction.
        store = MapStore(":memory:", suppression_key=b"test-key")
        with store.batch("bgscet"):
            store.cache_put("search", "k", [], 60)
        with store.batch("bgsit"):
            self.assertEqual(store.cache_get("search", "k"), [])

    def test_the_daily_digest_prunes_the_cache(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        store.cache_put("search", "old", [], 60, now=(NOW - timedelta(days=30)).isoformat())
        store.cache_put("search", "fresh", [], 86400)
        desk = IncidentDesk(store, recipients=lambda institution_id: (), notify=lambda *args: None)

        class Queue:
            handlers = {}

            def register(self, name, handler):
                self.handlers[name] = handler

        queue = Queue()
        register_handlers(queue, map_desk=desk)
        asyncio.run(queue.handlers["intelligence.map_digest"]({"institution_id": "bgscet"}))
        self.assertEqual([row["kind"] for row in store.backend.fetchall("SELECT kind FROM intel_shared_cache")], ["search"])
        self.assertIsNotNone(store.cache_get("search", "fresh"))


if __name__ == "__main__":
    unittest.main()
