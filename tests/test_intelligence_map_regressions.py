"""Regression tests for defects a review of the internet map's grading, engine and connectors found."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.connectors.feeds import FeedConnector
from app.internet_intelligence.map.connectors.web import LeadPageConnector, OfficialSiteConnector
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.harvest import OfficialSiteHarvester, is_contact_page
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.map.structure import BODY, FOOTER, HEADER, parse_structure
from app.internet_intelligence.profile import InstitutionProfile
from app.workers.handlers import register_handlers
from platform_fixtures import principal
from test_intelligence_map_engine import HOME, Site

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
PROFILE = InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])


def positions(html):
    return {link.href: link.position for link in parse_structure(html, "https://bgscet.ac.in/").links}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        synced = sync_profile(self.store, PROFILE)
        self.entity_id, self.domain_id = synced["entity_id"], synced["domains_added"][0]
        regrade(self.store, "bgscet", [self.domain_id])

    async def harvest(self, pages, **kwargs):
        return await OfficialSiteHarvester(Site(pages).fetcher(), self.store, **kwargs).harvest("bgscet", self.domain_id, run_id="r")

    def grade_of(self, key):
        asset = self.store.find_asset("bgscet", key)
        return asset["grade"] if asset else None


class PositionTests(unittest.TestCase):
    def test_only_the_pages_own_chrome_counts(self):
        divi = '<body class="home et_header_style_left et_pb_footer_columns4"><main><p>Congrats <a href="https://www.instagram.com/priya.personal/">Priya</a></p></main></body>'
        astra = '<body class="ast-hfb-header"><main><a href="https://www.instagram.com/priya.personal/">x</a></main></body>'
        article = '<main><article><p>news</p><footer>Posted by <a href="https://x.com/author_personal">author</a></footer></article></main>'
        card = '<main><div class="card"><div class="card-footer"><a href="https://www.linkedin.com/in/rahul-student">Rahul</a></div></div></main>'
        for html in (divi, astra, article, card):
            self.assertEqual(set(positions(html).values()), {BODY}, html[:40])
        chrome = '<body class="et_pb_footer_columns4"><header class="site-header"><a href="https://x.com/college">X</a></header><div id="colophon"><a href="https://www.instagram.com/college/">IG</a></div><footer><a href="https://www.facebook.com/college">FB</a></footer></body>'
        self.assertEqual(positions(chrome), {"https://x.com/college": HEADER, "https://www.instagram.com/college/": FOOTER, "https://www.facebook.com/college": FOOTER})

    def test_json_ld_same_as_is_the_organisations_only(self):
        html = """<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
        {"@type":"CollegeOrUniversity","url":"https://bgscet.ac.in/","sameAs":["https://www.instagram.com/bgscet_engg_coll/"],
         "founder":{"@type":"Person","sameAs":["https://www.linkedin.com/in/dr-x-personal"]}},
        {"@type":"Person","name":"Author","sameAs":["https://x.com/author_personal"]},
        {"@type":"Organization","url":"https://someone-else.example/","sameAs":["https://x.com/someone_else"]},
        {"@type":"WebSite","publisher":{"@type":"Organization","sameAs":["https://www.youtube.com/@bgscet"]}}]}</script>"""
        self.assertEqual(parse_structure(html, "https://bgscet.ac.in/").same_as, ["https://www.instagram.com/bgscet_engg_coll/", "https://www.youtube.com/@bgscet"])


class PersonalProfileTests(Base):
    async def test_staff_profiles_on_contact_pages_are_not_the_institutions(self):
        home = HOME.replace('<footer>', '<footer><a href="/about/principal/">Our principal</a> ')
        contact = """<html><body><main><p>TPO Dr A. Kumar <a href="https://www.linkedin.com/in/a-kumar-tpo">LinkedIn</a>,
        WhatsApp <a href="https://wa.me/919876543210">us</a>; the MBA school: <a href="https://www.instagram.com/bgscet_mba">@bgscet_mba</a></p></main></body></html>"""
        principal_page = '<html><body><main><a href="https://www.facebook.com/profile.php?id=100001234567">Dr Ramesh on Facebook</a></main></body></html>'
        await self.harvest({"https://bgscet.ac.in/": (200, home), "https://bgscet.ac.in/contact-us/": (200, contact), "https://bgscet.ac.in/about/principal/": (200, principal_page)})
        self.assertEqual(self.grade_of("instagram:bgscet_mba"), "A", "a contact page naming the institution's own account still counts")
        for key in ("linkedin:in:a-kumar-tpo", "whatsapp:919876543210", "facebook:id:100001234567"):
            self.assertIsNone(self.store.find_asset("bgscet", key), key)
        self.assertTrue(is_contact_page("https://bgscet.ac.in/contact-us/"))
        self.assertFalse(is_contact_page("https://bgscet.ac.in/about/principal/"), "a page beneath 'about' lists people")


class AnchorLossTests(Base):
    async def test_a_site_that_lapses_leaves_was_official_then(self):
        await self.harvest({"https://bgscet.ac.in/": (200, HOME)})
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "A")
        await self.harvest({"https://bgscet.ac.in/": (200, '<script>window.onload=function(){window.location.href="/lander"}</script>')})
        self.assertEqual(self.store.get_asset("bgscet", self.domain_id)["grade"], "D")
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "A-arch", "it was official then; the site no longer vouches")
        await self.harvest({"https://bgscet.ac.in/": (200, HOME)})
        self.assertEqual(self.store.get_asset("bgscet", self.domain_id)["grade"], "A", "a renewed domain recovers")
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "A")

    def test_takeover_rules(self):
        domain = {"kind": "domain"}

        def ev(kind, detail, at, polarity="refutes", via="live", channel="site:x"):
            return {"kind": kind, "detail": detail, "polarity": polarity, "observed_via": via, "channel": channel, "observed_at": at.isoformat(), "source_url": ""}

        configured = ev("configured_domain", "", NOW - timedelta(days=60), polarity="supports", via="reviewer", channel="profile")
        parked, clean = ev("integrity", "parked:lander", NOW - timedelta(days=30)), ev("integrity", "clean:", NOW - timedelta(days=1), polarity="supports")
        self.assertEqual(grade(domain, [configured, parked]).grade, "D")
        self.assertEqual(grade(domain, [configured, parked, clean]).grade, "A", "renewed: parked no longer")
        hijacked = ev("integrity", "hijacked:gambling", NOW - timedelta(days=30))
        self.assertEqual(grade(domain, [configured, hijacked, clean]).grade, "D", "a hijacker can serve a clean page")
        owner = ev("owner_claim", "verified", NOW, polarity="supports", via="owner", channel="owner:x")
        self.assertEqual(grade(domain, [configured, hijacked, clean, owner]).grade, "D", "the token is public: a re-registrant can republish it")
        reviewer = ev("reviewer_confirm", "recovered", NOW + timedelta(hours=1), polarity="supports", via="reviewer", channel="reviewer:x")
        self.assertEqual(grade(domain, [configured, hijacked, clean, owner, reviewer]).grade, "O", "a reviewer ends it; the owner's proof then counts again")
        dns_parked = ev("integrity", "parked:name servers", NOW - timedelta(hours=1), channel="dns")
        self.assertEqual(grade(domain, [configured, clean, dns_parked]).grade, "D", "each channel's latest word counts")


class FeedAndCostTests(Base):
    async def test_a_declared_youtube_feed_never_speaks_for_the_domain(self):
        home = HOME.replace('<link rel="alternate" type="application/rss+xml" href="/feed/">', '<link rel="alternate" type="application/rss+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id=UCaaaaaaaaaaaaaaaaaaaaaa">')
        from app.internet_intelligence.map.connectors.base import ConnectorContext

        context = ConnectorContext(self.store, "bgscet", "r", NOW, fetcher=Site({"https://bgscet.ac.in/": (200, home)}).fetcher())
        result = await OfficialSiteConnector().run({"target": self.domain_id, "hops": 0}, context)
        [feed] = [lead for lead in result.leads if lead.connector == "feed"]
        self.assertIsNone(feed.asset_id)
        before = [row for row in self.store.list_evidence("bgscet", asset_id=self.domain_id) if row["kind"] == "liveness"]
        gone = ConnectorContext(self.store, "bgscet", "r", NOW, fetcher=Site({}).fetcher())
        await FeedConnector(active=True).run({"target": feed.target, "asset_id": self.domain_id, "origin": "recurring"}, gone)
        after = [row for row in self.store.list_evidence("bgscet", asset_id=self.domain_id) if row["kind"] == "liveness"]
        self.assertEqual(len(after), len(before), "a deleted channel's feed does not make the site dead")

    async def test_a_harvest_makes_at_most_max_pages_requests_and_reports_them(self):
        links = " ".join(f'<a href="/about-us-{index}/">About {index}</a>' for index in range(40))
        seen = Site({"https://bgscet.ac.in/": (200, HOME.replace("<footer>", f"<footer>{links} ").replace('href="/contact-us/"', 'href="/contact"'))})
        harvest = await OfficialSiteHarvester(seen.fetcher(), self.store, max_pages=5).harvest("bgscet", self.domain_id)
        self.assertLessEqual(len(seen.seen), 5)
        self.assertEqual(harvest.requests, len(seen.seen))


class SubdomainAnchorTests(Base):
    async def test_an_inferred_subdomain_cannot_vouch_for_accounts(self):
        from app.internet_intelligence.map.connectors.base import ConnectorContext

        erp = '<html><head><title>Student ERP</title></head><body><footer>Powered by <a href="https://www.facebook.com/edusofterp">EduSoft</a></footer></body></html>'
        context = ConnectorContext(self.store, "bgscet", "r", NOW, fetcher=Site({"https://erp.bgscet.ac.in/": (200, erp)}).fetcher())
        await LeadPageConnector().run({"target": "https://erp.bgscet.ac.in/", "hops": 1}, context)
        erp_asset = self.store.find_asset("bgscet", "web:erp.bgscet.ac.in")
        self.assertEqual(erp_asset["grade"], "B")
        harvest = await OfficialSiteHarvester(Site({"https://erp.bgscet.ac.in/": (200, erp)}).fetcher(), self.store).harvest("bgscet", erp_asset["asset_id"])
        self.assertEqual(harvest.anchor, "C", "an inferred site never anchors")
        vendor = self.store.find_asset("bgscet", "facebook:edusofterp")
        # What it links is one grade below it (a hub's link, not an official one), and only a candidate.
        self.assertEqual((vendor["grade"], vendor["relation"]), ("C", "unknown"))
        self.assertEqual({row["kind"] for row in self.store.list_evidence("bgscet", asset_id=vendor["asset_id"])}, {"hub_link"})
        engine = MapEngine(self.store, ConnectorRegistry([OfficialSiteConnector()]), clock=lambda: NOW)
        engine.ensure_recurring("bgscet", None)
        self.assertEqual(sorted(row["target"] for row in self.store.list_sources("bgscet", connector="official_site")), sorted([self.domain_id, erp_asset["asset_id"]]), "a live official subdomain is watched too")


class CacheTests(Base):
    async def test_validators_are_per_institution_and_a_bare_304_is_not_trusted(self):
        site = Site({"https://bgscet.ac.in/": (200, HOME)}, etags=True)
        other = MapStore(backend=self.store.backend, suppression_key=b"test-key")
        other_domain = sync_profile(other, InstitutionProfile("other", PROFILE.name, "Bengaluru", official_domains=["bgscet.ac.in"]))["domains_added"][0]
        regrade(other, "other", [other_domain])
        await OfficialSiteHarvester(site.fetcher(), other, conditional=True).harvest("other", other_domain)
        await OfficialSiteHarvester(site.fetcher(), self.store, conditional=True).harvest("bgscet", self.domain_id)
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "A", "another institution's reading of the page does not hide it")
        # Our own validators: an unchanged page is re-confirmed from a 304.
        again = await OfficialSiteHarvester(site.fetcher(), self.store, conditional=True).harvest("bgscet", self.domain_id)
        self.assertEqual(again.integrity, "unchanged")


class ReviewAndDisputeTests(Base):
    def test_rejecting_an_impersonator_keeps_the_platforms_other_sources(self):
        fake, _ = self.store.upsert_asset("bgscet", asset_ref("https://www.youtube.com/@bgscet-official-fake"), entity_id=self.entity_id)
        self.store.upsert_source("bgscet", connector="feed", target="https://www.youtube.com/feeds/videos.xml?channel_id=UCaaaaaaaaaaaaaaaaaaaaaa", origin="recurring")
        review_id, _ = self.store.add_review_item("bgscet", kind="impersonation_candidate", title="fake", asset_id=fake)
        MapService(self.store).decide(principal(PrincipalType.PRINCIPAL, college_id="bgscet"), "bgscet", review_id, decision="impersonation")
        self.assertEqual(self.store.list_sources("bgscet", connector="feed")[0]["status"], "active")

    def test_forgetting_a_person_redacts_every_item_that_named_them(self):
        person, _ = self.store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/some_student_name/"), entity_id=self.entity_id)
        first, _ = self.store.add_review_item("bgscet", kind="impersonation_candidate", title="@some_student_name calls itself official", asset_id=person, url="https://www.instagram.com/some_student_name/")
        second, _ = self.store.add_review_item("bgscet", kind="dispute", title="More than one official: @some_student_name", asset_id=person, url="https://www.instagram.com/some_student_name/")
        self.store.upsert_incident("bgscet", kind="impersonation_confirmed", target="instagram:some_student_name", severity="high", title="x")
        MapService(self.store).decide(principal(PrincipalType.PRINCIPAL, college_id="bgscet"), "bgscet", first, decision="personal")
        for review_id in (first, second):
            item = self.store.get_review_item("bgscet", review_id)
            self.assertEqual((item["status"], item["url"]), ("decided", ""))
            self.assertNotIn("some_student_name", item["title"])
        self.assertEqual(self.store.list_incidents("bgscet"), [])

    def test_a_dispute_ends_when_one_account_is_anchored_or_the_other_rejected(self):
        ids = []
        for handle in ("bgscet_one", "bgscet_two"):
            asset_id, _ = self.store.upsert_asset("bgscet", asset_ref(f"https://www.instagram.com/{handle}/"), entity_id=self.entity_id, relation="official")
            self.store.add_evidence("bgscet", asset_id=asset_id, kind="reviewer_confirm", channel="reviewer:x", observed_via="reviewer")
            ids.append(asset_id)
        regrade(self.store, "bgscet", ids)
        self.assertEqual({self.store.get_asset("bgscet", asset_id)["status"] for asset_id in ids}, {"disputed"})
        self.store.add_evidence("bgscet", asset_id=ids[1], kind="reviewer_reject", polarity="refutes", channel="reviewer:x", observed_via="reviewer")
        regrade(self.store, "bgscet", [ids[1]])
        self.assertNotEqual(self.store.get_asset("bgscet", ids[0])["status"], "disputed", "a rejected impostor disputes nothing, and the survivor is cleared too")


class BudgetTests(unittest.TestCase):
    def test_a_spent_budget_does_not_starve_other_work(self):
        store = MapStore(":memory:", suppression_key=b"test-key")

        class Fake:
            access_mode, max_grade, default_interval = "public_page", "C", 86400

            def __init__(self, name, budget):
                self.name, self.budget_key, self.runs = name, budget, 0

            def enabled(self):
                return True

            def cost(self, source):
                return 1.0

            async def run(self, source, context):
                self.runs += 1
                return ConnectorResult(outcome="ok")

        searcher, watcher = Fake("searcher", "search"), Fake("watcher", "fetch")
        for index in range(10):
            store.upsert_source("bgscet", connector="searcher", target=f"s{index}", origin="recurring", due_at=(NOW - timedelta(days=2)).isoformat())
        store.upsert_source("bgscet", connector="watcher", target="w", origin="recurring", due_at=NOW.isoformat())
        engine = MapEngine(store, ConnectorRegistry([searcher, watcher]), EngineConfig(sources_per_tick=2, budgets={"search": 2, "fetch": 100}, class_split=(("rotation", 1.0),)), clock=lambda: NOW)
        asyncio.run(engine.tick("bgscet"))
        self.assertEqual((searcher.runs, watcher.runs), (1, 1), "one search fits the tenant share; the watch still runs")
        asyncio.run(engine.tick("bgscet"))
        self.assertEqual(searcher.runs, 1, "a spent budget's sources are not even claimed")


class TickJobTests(unittest.TestCase):
    def test_the_job_row_keeps_counts_not_review_content(self):
        store = MapStore(":memory:", suppression_key=b"test-key")

        class Court:
            name, access_mode, budget_key, max_grade, default_interval = "court", "official_api", "indiankanoon", "C", 86400

            def enabled(self):
                return True

            def cost(self, source):
                return 0

            async def run(self, source, context):
                return ConnectorResult(outcome="ok", review=[{"kind": "court_record", "title": "State v. Someone", "url": "https://indiankanoon.org/doc/1/"}])

        store.upsert_source("bgscet", connector="court", target="t", due_at=NOW.isoformat())

        class Queue:
            handlers = {}

            def register(self, name, handler):
                self.handlers[name] = handler

        queue = Queue()
        register_handlers(queue, map_engine=MapEngine(store, ConnectorRegistry([Court()]), clock=lambda: NOW))
        result = asyncio.run(queue.handlers["intelligence.map_tick"]({"institution_id": "bgscet", "requested_by": "p"}))
        self.assertEqual(result["counts"]["review"], 1)
        self.assertNotIn("review", result)
        self.assertNotIn("indiankanoon.org/doc", str(result))
        self.assertNotIn("State v. Someone", str(result))


if __name__ == "__main__":
    unittest.main()
