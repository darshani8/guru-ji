"""How the map crawls: one request at a time per site, sitemaps, unnamed official sites, page health, CSS-hidden spam, lapsed names, body hashes."""

import asyncio
import contextlib
import hashlib
import os
import socket
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import httpx

from app.internet_intelligence.fetch import MAX_CRAWL_DELAY_SECONDS, PublicPageFetcher, resolve_host
from app.internet_intelligence.map import store as store_module
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry
from app.internet_intelligence.map.connectors.hubs import DirectoryConnector
from app.internet_intelligence.map.connectors.web import LeadPageConnector, OfficialSiteConnector, RecheckConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.integrity import assess
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.store import GLOBAL_TABLES, TENANT_TABLES, MapStore
from app.internet_intelligence.map.structure import HIDDEN, parse_sitemap, parse_structure
from test_intelligence_map_engine import CONTACT, HOME, NOW, PROFILE, PUBLIC_IP, Clock, Site

POSTGRES_URL = os.environ.get("GURU_TEST_POSTGRES_URL", "")
PARKED = "<html><head><title>acmbgs.org</title></head><body><p>This domain is for sale! Buy this domain.</p></body></html>"
GAMBLING = "<html><head><title>Slot Gacor Maxwin Togel</title></head><body><p>situs slot gacor hari ini, togel, judi online, maxwin</p></body></html>"
CSE = """<html><head><title>Department of CSE - BGS College of Engineering and Technology</title></head><body><p>CSE department</p>
<footer><a href="https://www.instagram.com/bgscet_cse/">Instagram</a></footer></body></html>"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def with_robots(site: Site, robots: str) -> Site:
    plain = site.handler
    site.handler = lambda request: httpx.Response(200, text=robots, request=request) if request.url.path == "/robots.txt" else plain(request)  # type: ignore[method-assign]
    return site


@contextlib.contextmanager
def on_day(days: float):
    """Evidence written inside is stamped ``days`` after NOW."""

    with mock.patch.object(store_module, "now_iso", lambda: (NOW + timedelta(days=days)).isoformat()):
        yield


class MapCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        synced = sync_profile(self.store, PROFILE)
        self.entity_id, self.domain_id = synced["entity_id"], synced["domains_added"][0]
        regrade(self.store, "bgscet", [self.domain_id])

    def evidence(self, key: str) -> list[dict]:
        return self.store.list_evidence("bgscet", asset_id=self.store.find_asset("bgscet", key)["asset_id"])


class FakeTime:
    """A clock that moves only when the fetcher sleeps: spacing is exact and nothing waits for real."""

    def __init__(self) -> None:
        self.now, self.slept = 1000.0, []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class PolitenessTests(unittest.IsolatedAsyncioTestCase):
    def fetcher(self, robots: str = "User-agent: *\nCrawl-delay: 30\n", **kwargs) -> PublicPageFetcher:
        self.time, self.stamps, self.inflight, self.peak = FakeTime(), [], {}, {}

        async def handler(request: httpx.Request) -> httpx.Response:
            host = request.headers["host"]
            self.stamps.append((host, request.url.path, self.time.now))
            self.inflight[host] = self.inflight.get(host, 0) + 1
            self.peak[host] = max(self.peak.get(host, 0), self.inflight[host])
            for _ in range(3):
                await asyncio.sleep(0)  # give any other request to the host its chance to start
            self.inflight[host] -= 1
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=robots, request=request)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=CONTACT, request=request)

        return PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: (PUBLIC_IP,), clock=self.time, sleep=self.time.sleep, **kwargs)

    def gaps(self, host: str = "bgscet.ac.in") -> list[float]:
        times = [at for seen, _, at in self.stamps if seen == host]
        return [later - earlier for earlier, later in zip(times, times[1:])]

    async def test_requests_to_a_site_are_spaced_by_its_crawl_delay(self):
        fetcher = self.fetcher()
        for path in ("/a", "/b", "/c"):
            self.assertTrue((await fetcher.retrieve(f"https://bgscet.ac.in{path}")).ok)
        self.assertEqual([path for _, path, _ in self.stamps], ["/robots.txt", "/a", "/b", "/c"])
        self.assertEqual(self.gaps(), [30.0, 30.0, 30.0], "robots.txt and every page after it are 30 s apart")

    async def test_one_request_at_a_time_per_site(self):
        fetcher = self.fetcher()
        results = await asyncio.gather(*(fetcher.retrieve(f"https://bgscet.ac.in/{index}") for index in range(3)), fetcher.retrieve("https://other.example/x"))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(self.peak, {"bgscet.ac.in": 1, "other.example": 1}, "never two requests in flight to one host")
        self.assertTrue(self.gaps() and all(gap >= 30 for gap in self.gaps()), self.gaps())

    async def test_the_delay_is_capped_and_a_minimum_gap_applies_without_one(self):
        fetcher = self.fetcher(robots="User-agent: *\nCrawl-delay: 3600\n")
        for path in ("/a", "/b"):
            await fetcher.retrieve(f"https://bgscet.ac.in{path}")
        self.assertEqual(self.gaps(), [MAX_CRAWL_DELAY_SECONDS, MAX_CRAWL_DELAY_SECONDS])
        fetcher = self.fetcher(robots="User-agent: *\nAllow: /\n")
        for path in ("/a", "/b"):
            await fetcher.retrieve(f"https://bgscet.ac.in{path}")
        self.assertEqual(self.gaps(), [1.0, 1.0], "a second apart by default")

    async def test_a_crawl_delay_for_our_crawler_by_name_is_honoured(self):
        fetcher = self.fetcher(robots="User-agent: GuruJi-InstitutionIntelligence/1.0\nCrawl-delay: 20\n\nUser-agent: *\nCrawl-delay: 5\n")
        for path in ("/a", "/b"):
            await fetcher.retrieve(f"https://bgscet.ac.in{path}")
        self.assertEqual(self.gaps(), [20.0, 20.0], "the group naming our product token (with or without a version) is the one that applies")

    async def test_another_process_holding_the_site_is_waited_for(self):
        answers, calls = [7.0], []

        def slots(host: str, interval: float) -> float:
            calls.append((host, interval))
            return answers.pop(0) if answers else 0.0

        fetcher = self.fetcher(robots="User-agent: *\n", host_slots=slots)
        self.assertTrue((await fetcher.retrieve("https://bgscet.ac.in/a")).ok)
        self.assertEqual(calls, [("bgscet.ac.in", 1.0)] * 3, "robots.txt and the page each claim the host; the refused claim is retried")
        self.assertIn(7.0, self.time.slept, "the other process's slot is waited out")


class HostSlotStoreTests(unittest.TestCase):
    def test_a_host_is_claimed_once_per_interval_platform_wide(self):
        store = MapStore(":memory:", suppression_key=b"k")
        self.assertEqual(store.claim_host_slot("BGSCET.ac.in.", 30, now=100.0), 0.0)
        self.assertEqual(store.claim_host_slot("bgscet.ac.in", 30, now=110.0), 20.0, "the same host, whatever its spelling")
        self.assertEqual(store.claim_host_slot("other.example", 30, now=110.0), 0.0, "hosts are independent")
        self.assertEqual(store.claim_host_slot("bgscet.ac.in", 30, now=130.0), 0.0)
        self.assertIn("intel_host_slots", GLOBAL_TABLES)
        self.assertNotIn("intel_host_slots", TENANT_TABLES)


@unittest.skipUnless(POSTGRES_URL, "set GURU_TEST_POSTGRES_URL to run the PostgreSQL tests")
class PostgresHostSlotTests(unittest.TestCase):
    def test_one_worker_wins_each_slot(self):
        stores = [MapStore(POSTGRES_URL, suppression_key=b"k") for _ in range(4)]
        host, now = f"pgt-{uuid4().hex[:10]}.example", time.time()
        barrier, waits = threading.Barrier(len(stores)), []

        def claim(store: MapStore) -> None:
            barrier.wait()
            waits.append(store.claim_host_slot(host, 30, now=now))

        threads = [threading.Thread(target=claim, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(wait == 0.0 for wait in waits), [False, False, False, True])
        self.assertAlmostEqual(max(waits), 30.0, places=3)
        self.assertEqual(stores[0].claim_host_slot(host, 30, now=now + 30), 0.0)


class SitemapTests(MapCase):
    def test_a_sitemap_is_read_without_declarations(self):
        urlset = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.example/follow-us/</loc></url><url><loc>javascript:x</loc></url></urlset>'
        index = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://a.example/page-sitemap.xml</loc></sitemap></sitemapindex>'
        self.assertEqual(parse_sitemap(urlset), (["https://a.example/follow-us/"], []))
        self.assertEqual(parse_sitemap(index), ([], ["https://a.example/page-sitemap.xml"]))
        entity = b'<?xml version="1.0"?><!DOCTYPE u [<!ENTITY e "https://a.example/x">]><urlset><url><loc>&e;</loc></url></urlset>'
        for refused in (entity, b"<rss><channel/></rss>", b"not xml"):
            self.assertEqual(parse_sitemap(refused), ([], []))

    async def test_a_follow_page_listed_only_in_the_sitemap_is_harvested(self):
        index = '<sitemapindex><sitemap><loc>https://bgscet.ac.in/post-sitemap.xml</loc></sitemap><sitemap><loc>https://bgscet.ac.in/page-sitemap.xml</loc></sitemap></sitemapindex>'
        pages = (
            '<urlset><url><loc>https://bgscet.ac.in/</loc></url><url><loc>https://bgscet.ac.in/about/principal/</loc></url>'
            '<url><loc>https://elsewhere.example/follow-us/</loc></url><url><loc>https://bgscet.ac.in/follow-us/</loc></url></urlset>'
        )
        follow = "<html><head><title>Follow us</title></head><body><main><p>Follow BGSCET</p><a href='https://www.instagram.com/bgscet_official/'>Instagram</a></main></body></html>"
        site = with_robots(Site({
            "https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT), "https://bgscet.ac.in/follow-us/": (200, follow),
            "https://bgscet.ac.in/sitemap_index.xml": (200, index, "application/xml"), "https://bgscet.ac.in/page-sitemap.xml": (200, pages, "text/xml"),
        }), "User-agent: *\nSitemap: https://bgscet.ac.in/sitemap_index.xml\n")
        result = await OfficialSiteHarvester(site.fetcher(), self.store, max_pages=5).harvest("bgscet", self.domain_id)
        fetched = [url for url, _ in site.seen]
        self.assertEqual(fetched, [
            "https://bgscet.ac.in/", "https://bgscet.ac.in/sitemap_index.xml", "https://bgscet.ac.in/page-sitemap.xml", "https://bgscet.ac.in/contact-us/", "https://bgscet.ac.in/follow-us/",
        ], "an index's page sitemap first; nothing on another host")
        self.assertEqual(result.requests, 5, "sitemap reads count against max_pages")
        self.assertIn("instagram:bgscet_official", result.accounts)
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_official")["grade"], "A")
        small = with_robots(Site({"https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT)}), "User-agent: *\nSitemap: https://bgscet.ac.in/s.xml\n")
        await OfficialSiteHarvester(small.fetcher(), self.store, max_pages=2).harvest("bgscet", self.domain_id)
        self.assertEqual([url for url, _ in small.seen], ["https://bgscet.ac.in/", "https://bgscet.ac.in/contact-us/"], "no sitemap read without room for a page after it")


class UnnamedOfficialSiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_live_official_subdomain_is_crawled_and_its_footer_account_mapped_at_c(self):
        store, clock = MapStore(":memory:", suppression_key=b"k"), Clock()
        site = Site({"https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT), "https://cse.bgscet.ac.in/": (200, CSE)})
        engine = MapEngine(store, ConnectorRegistry([OfficialSiteConnector(), LeadPageConnector(), RecheckConnector()]), EngineConfig(), fetcher=site.fetcher(), clock=clock, profile_loader=lambda institution: PROFILE)
        await engine.tick("bgscet")
        store.upsert_source("bgscet", connector="lead_page", target="https://cse.bgscet.ac.in/", origin="lead", work_class="explore", hops=1, due_at=clock().isoformat())
        for _ in range(2):  # the lead records the subdomain (B); the next tick watches and reads it
            clock.advance(days=8)
            await engine.tick("bgscet")
        subdomain = store.find_asset("bgscet", "web:cse.bgscet.ac.in")
        self.assertEqual((subdomain["grade"], subdomain["relation"]), ("B", "official"))
        self.assertIn(subdomain["asset_id"], [row["target"] for row in store.list_sources("bgscet", connector="official_site")])
        account = store.find_asset("bgscet", "instagram:bgscet_cse")
        self.assertEqual(account["grade"], "C", "one grade below the B subdomain")
        self.assertEqual({(row["kind"], row["detail"]) for row in store.list_evidence("bgscet", asset_id=account["asset_id"])}, {("hub_link", "B:footer")}, "an inferred site never gives official_link")
        self.assertEqual(store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "A", "the named site still vouches")


class PageHealthTests(MapCase):
    async def test_a_recheck_reads_the_page_not_just_the_status(self):
        url = "https://acmbgs.org/events/2024"
        page, _ = self.store.upsert_asset("bgscet", asset_ref(url), entity_id=self.entity_id, relation="official")
        self.store.add_evidence("bgscet", asset_id=page, kind="imported_claim", detail="sweep", channel="seed", observed_via="import")
        context = ConnectorContext(self.store, "bgscet", "run", NOW, fetcher=Site({url: (200, PARKED)}).fetcher())
        result = await RecheckConnector().run({"target": page}, context)
        asset = self.store.get_asset("bgscet", page)
        self.assertEqual((result.outcome, result.failed, asset["status"], asset["grade"], asset["last_verified_at"]), ("parked", True, "parked", "D", None))
        rows = {row["kind"]: row for row in self.store.list_evidence("bgscet", asset_id=page)}
        self.assertEqual((rows["liveness"]["polarity"], rows["liveness"]["detail"]), ("refutes", "parked:200"))
        self.assertEqual((rows["integrity"]["polarity"], rows["integrity"]["raw_sha256"]), ("refutes", sha(PARKED)))
        self.assertEqual([incident["kind"] for incident in result.incidents], ["site_parked"])
        # A healthy page is live, with its integrity and the hash of what was read.
        healthy = "<html><head><title>ACM BGS</title></head><body><p>Events</p></body></html>"
        await RecheckConnector().run({"target": page}, ConnectorContext(self.store, "bgscet", "run", NOW, fetcher=Site({url: (200, healthy)}).fetcher()))
        latest = self.store.list_evidence("bgscet", asset_id=page)[-2:]
        self.assertEqual({(row["kind"], row["polarity"], row["raw_sha256"]) for row in latest}, {("integrity", "supports", sha(healthy)), ("liveness", "supports", sha(healthy))})

    async def test_a_hijacked_official_subdomain_is_recorded_and_reported_not_pruned(self):
        context = ConnectorContext(self.store, "bgscet", "run", NOW, fetcher=Site({"https://fest.bgscet.ac.in/": (200, GAMBLING)}).fetcher())
        result = await LeadPageConnector().run({"target": "https://fest.bgscet.ac.in/", "hops": 1}, context)
        self.assertEqual((result.outcome, result.prune), ("unhealthy", False))
        self.assertEqual([(incident["kind"], incident["target"]) for incident in result.incidents], [("site_hijacked", "fest.bgscet.ac.in")])
        asset = self.store.find_asset("bgscet", "web:fest.bgscet.ac.in")
        self.assertEqual((asset["relation"], asset["status"], asset["grade"]), ("official", "hijacked", "D"))
        integrity = [row for row in self.evidence("web:fest.bgscet.ac.in") if row["kind"] == "integrity"]
        self.assertEqual([(row["polarity"], row["raw_sha256"]) for row in integrity], [("refutes", sha(GAMBLING))])
        # An unhealthy site that is not the institution's own is still just a dud lead.
        other = await LeadPageConnector().run({"target": "https://casino.example/", "hops": 1}, ConnectorContext(self.store, "bgscet", "run", NOW, fetcher=Site({"https://casino.example/": (200, GAMBLING)}).fetcher()))
        self.assertEqual((other.outcome, other.prune, other.incidents), ("unhealthy", True, []))


class HiddenByStylesheetTests(unittest.TestCase):
    def page(self, style: str, attributes: str) -> str:
        return (
            f'<html><head><title>BGSCET</title><style>{style}</style></head><body><p>Welcome</p><div {attributes}><a href="https://replica-watches.example/">replica rolex</a> '
            '<a href="https://gacor.example/">slot gacor</a></div><footer><a href="https://www.instagram.com/bgscet_engg_coll/">Instagram</a></footer></body></html>'
        )

    def status(self, style: str, attributes: str) -> str:
        html = self.page(style, attributes)
        return assess(parse_structure(html, "https://bgscet.ac.in/"), html).status

    def test_spam_hidden_with_a_class_makes_the_page_compromised(self):
        for style, attributes in (
            (".x9{display:none}", 'class="x9"'), ("/* seo */ #promo, .other { position:absolute; left:-9999px }", 'id="promo"'), ("", 'class="d-none"'),
            ("", 'class="wrap sr-only"'), ("", 'class="screen-reader-text"'), ("div.x9 { color: red } .x9 { visibility: hidden }", 'class="x9"'),
        ):
            self.assertEqual(self.status(style, attributes), "compromised", (style, attributes))

    def test_menus_a_stylesheet_shows_again_are_not_hidden(self):
        for style, attributes in (
            ("", 'class="d-none d-md-block"'), ("", 'class="hidden lg:flex"'), ("", 'class="hidden group-hover:block"'), ("@media (max-width: 600px) { .x9 { display:none } }", 'class="x9"'),
            (".x9{display:none} .open .x9{display:block}", 'class="x9"'), (".nav .x9{display:none}", 'class="x9"'),
        ):
            self.assertEqual(self.status(style, attributes), "clean", (style, attributes))
        html = self.page(".x9{display:none}", 'class="x9"').replace("<footer>", '<footer class="x9">')
        self.assertEqual({link.position for link in parse_structure(html, "https://bgscet.ac.in/").links if "instagram" in link.href}, {HIDDEN}, "a hidden footer vouches for nothing")


class LapsedNameTests(MapCase):
    def fetcher(self, resolver, handler=None) -> PublicPageFetcher:
        handler = handler or (lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=HOME, request=request))
        return PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=resolver, min_host_interval=0)

    async def test_a_lapsed_name_a_private_host_and_a_refusing_host_are_told_apart(self):
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request) from ConnectionRefusedError(111, "Connection refused")

        def unreachable_network(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network is unreachable", request=request) from OSError(101, "Network is unreachable")

        def failing(host: str):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        # Only a refusal says nothing listens there; any other failed connection is an outage (here, while reading robots.txt).
        cases = [(lambda host: (), None, "unresolved"), (lambda host: ("10.0.0.1",), None, "not_public"), (failing, None, "error"), (lambda host: (PUBLIC_IP,), refused, "unreachable"), (lambda host: (PUBLIC_IP,), unreachable_network, "robots")]
        for resolver, handler, outcome in cases:
            self.assertEqual((await self.fetcher(resolver, handler).retrieve("https://bgscet.ac.in/")).outcome, outcome)

    def test_the_system_resolver_tells_no_such_name_from_an_outage(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known")):
            self.assertEqual(resolve_host("lapsed.example"), ())
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")):
            with self.assertRaises(socket.gaierror):
                resolve_host("flaky.example")

    async def test_a_domain_whose_name_lapsed_dies_and_what_it_linked_becomes_history(self):
        resolves = [True]
        fetcher = Site({"https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT)}).fetcher()
        fetcher.resolver = lambda host: (PUBLIC_IP,) if resolves[0] else ()
        with on_day(0):
            await OfficialSiteHarvester(fetcher, self.store).harvest("bgscet", self.domain_id)
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "A")
        resolves[0] = False
        for day in (7, 8):
            with on_day(day):
                result = await OfficialSiteHarvester(fetcher, self.store).harvest("bgscet", self.domain_id)
        domain = self.store.get_asset("bgscet", self.domain_id)
        self.assertEqual((result.pages[0]["outcome"], domain["status"], domain["grade"]), ("unresolved", "dead", "D"))
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "A-arch", "was official then")

    def test_two_refusals_a_day_apart_are_dead(self):
        def liveness(hours: float, reason: str) -> dict:
            return {"evidence_id": f"e{hours}", "kind": "liveness", "polarity": "refutes", "detail": f"{reason}:None", "observed_at": (NOW + timedelta(hours=hours)).isoformat(), "observed_via": "live", "channel": "fetch"}

        configured = {"evidence_id": "c", "kind": "configured_domain", "polarity": "supports", "detail": "profile", "observed_at": NOW.isoformat(), "observed_via": "reviewer", "channel": "profile"}
        for reason in ("unreachable", "unresolved"):
            self.assertEqual(grade({"kind": "domain"}, [configured, liveness(1, reason), liveness(26, reason)], now=NOW + timedelta(days=2)).status, "dead")
            self.assertIsNone(grade({"kind": "domain"}, [configured, liveness(1, reason), liveness(2, reason)], now=NOW + timedelta(days=2)).status, "not yet a day apart")


class BodyHashTests(MapCase):
    async def test_evidence_carries_the_hash_of_the_page_it_came_from(self):
        await OfficialSiteHarvester(Site({"https://bgscet.ac.in/": (200, HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT)}).fetcher(), self.store).harvest("bgscet", self.domain_id)
        domain_rows = self.store.list_evidence("bgscet", asset_id=self.domain_id)
        self.assertEqual({row["raw_sha256"] for row in domain_rows if row["kind"] in {"liveness", "integrity"}}, {sha(HOME)})
        self.assertEqual({(row["kind"], row["raw_sha256"]) for row in self.evidence("instagram:bgscet_engg_coll")}, {("official_link", sha(HOME))})


class SnippetOnlyTests(MapCase):
    async def test_snippet_only_sites_are_never_fetched_and_their_leads_are_dropped(self):
        fetcher = Site({}).fetcher()
        self.assertFalse(fetcher.allowed_domain("https://redd.it/abc123"))
        context = ConnectorContext(self.store, "bgscet", "run", NOW, fetcher=fetcher)
        lead = await LeadPageConnector().run({"target": "https://redd.it/abc123", "hops": 1}, context)
        directory = await DirectoryConnector(active=True).run({"target": "https://www.instagram.com/bgscet_engg_coll/", "origin": "seed"}, context)
        self.assertEqual([(result.outcome, result.prune) for result in (lead, directory)], [("snippet_only", True), ("snippet_only", True)])


if __name__ == "__main__":
    unittest.main()
