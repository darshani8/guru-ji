"""The connector findings of the fourth review: robots-respecting sources, news, look-alikes and escalation."""

import unittest
from datetime import timedelta

from app.api.platform_runtime import map_connectors
from app.config.settings import AppSettings
from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import YouTubeConnector
from app.internet_intelligence.map.connectors.archive import HOSTS_SUFFIX, WaybackConnector
from app.internet_intelligence.map.connectors.base import ConnectorRegistry
from app.internet_intelligence.map.connectors.feeds import NewsFeed, NewsFeedConnector, load_news_feeds
from app.internet_intelligence.map.connectors.hubs import DirectoryConnector, authority_of
from app.internet_intelligence.map.connectors.infrastructure import DnsConnector, RdapConnector
from app.internet_intelligence.map.connectors.lookalikes import LookalikeDomainConnector, variants
from app.internet_intelligence.map.connectors.places import GooglePlayConnector, GooglePlaySearchConnector, OpenStreetMapConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.export import export_tsv
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.incidents import IncidentDesk
from app.internet_intelligence.map.pipeline import nominated, regrade, sync_profile
from app.internet_intelligence.map.service import DECISIONS, MapService
from app.internet_intelligence.map.store import REVIEW_KINDS
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.search import SearchHit, StaticSearchProvider
from platform_fixtures import principal
from test_intelligence_map_connectors import INSTITUTION, NIRF, OLD_HOME, YT_CHANNEL, Fixture, api
from test_intelligence_map_engine import NOW, Site
from test_intelligence_map_places import LISTING, NOMINATIM, PLACE

CHANNELS = ("www.googleapis.com", "/youtube/v3/channels")
UPLOADS = ("www.googleapis.com", "/youtube/v3/playlistItems")
KANNADA_NAME = "ಬಿಜಿಎಸ್ ಎಂಜಿನಿಯರಿಂಗ್ ಕಾಲೇಜು"


def channel_body(channel_id=YT_CHANNEL, description="Official channel.", uploads=True):
    channel = {"id": channel_id, "snippet": {"title": "BGSCET", "description": description}, "statistics": {"subscriberCount": "1520"}}
    if uploads:
        channel["contentDetails"] = {"relatedPlaylists": {"uploads": "UU" + channel_id[2:]}}
    return {"items": [channel]}


class YouTubeDataApiTests(Fixture):
    def channel(self, url=f"https://www.youtube.com/channel/{YT_CHANNEL}", *, anchored=False):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation="official")
        if anchored:
            self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=self.domain_id, channel="site:bgscet.ac.in")
            regrade(self.store, INSTITUTION, [asset_id])
        return asset_id

    async def test_the_last_upload_is_read_from_the_uploads_playlist(self):
        channel_id = self.channel()
        playlist = {"items": [{"contentDetails": {"videoId": "a1", "videoPublishedAt": "2026-08-30T10:00:00Z"}}]}
        client = api({CHANNELS: (200, channel_body()), UPLOADS: (200, playlist)})
        connector = YouTubeConnector(api_key="k", active=True, client=client)
        result = await connector.run({"target": channel_id}, self.context())
        self.assertIn("contentDetails", client.seen[0].url.params["part"])
        self.assertEqual((client.seen[-1].url.params["playlistId"], client.seen[-1].url.params["maxResults"]), ("UU" + YT_CHANNEL[2:], "1"))
        self.assertEqual((result.cost, connector.cost({})), (2.0, 2.0), "the playlist read is one more unit")
        self.assertEqual(self.store.get_asset(INSTITUTION, channel_id)["last_activity_at"][:10], "2026-08-30")
        dormant = await YouTubeConnector(api_key="k", active=True, client=client).run({"target": channel_id}, self.context(now=NOW + timedelta(days=800)))
        self.assertTrue(any(note.startswith("dormant") for note in dormant.notes))
        bare = await YouTubeConnector(api_key="k", active=True, client=api({CHANNELS: (200, channel_body(uploads=False))})).run({"target": channel_id}, self.context())
        self.assertEqual(bare.cost, 1.0, "no uploads playlist, no second call")

    async def test_a_handle_that_now_names_another_channel_loses_its_grade(self):
        handle_id = self.channel("https://www.youtube.com/@bgscet", anchored=True)
        self.assertEqual(self.store.get_asset(INSTITUTION, handle_id)["grade"], "A")
        self.store.set_observation(INSTITUTION, handle_id, platform_id=YT_CHANNEL)
        other = "UC" + "b" * 22
        connector = YouTubeConnector(api_key="k", active=True, client=api({CHANNELS: (200, channel_body(other, description="Links: https://linktr.ee/somebody"))}))
        result = await connector.run({"target": handle_id, "hops": 0}, self.context())
        self.assertEqual([(item["kind"], item["asset_id"]) for item in result.review], [("handle_reassigned", handle_id)])
        self.assertEqual(result.leads, [], "a reassigned handle vouches for nothing")
        self.assertIn("identity_changed", {row["kind"] for row in self.store.list_evidence(INSTITUTION, asset_id=handle_id)})
        regrade(self.store, INSTITUTION, [handle_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, handle_id)["grade"], "C", "the footer link spoke for the old channel")
        again = await connector.run({"target": handle_id, "hops": 0}, self.context())
        self.assertEqual(again.review, [], "the new ID is stored; it is reported once")
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="handle_reassigned", title="x", asset_id=handle_id)
        MapService(self.store).decide(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION, review_id, decision="confirm")
        self.assertEqual(self.store.get_asset(INSTITUTION, handle_id)["grade"], "B", "a reviewer re-links it")

    async def test_a_verified_channel_passes_on_the_links_in_its_description(self):
        channel_id = self.channel(anchored=True)
        description = "Admissions: https://admissions.bgscet-portal.example/apply Links: https://linktr.ee/bgscet_links Site: https://www.bgscet.ac.in/"
        client = api({CHANNELS: (200, channel_body(description=description, uploads=False))})
        result = await YouTubeConnector(api_key="k", active=True, client=client).run({"target": channel_id, "hops": 0}, self.context())
        hub = self.store.find_asset(INSTITUTION, "linktree:bgscet_links")
        self.assertEqual(sorted((lead.connector, lead.target, lead.hops) for lead in result.leads), [("lead_page", "https://admissions.bgscet-portal.example/apply", 1), ("link_hub", hub["asset_id"], 1)])
        regrade(self.store, INSTITUTION, [hub["asset_id"]])
        self.assertEqual(self.store.get_asset(INSTITUTION, hub["asset_id"])["grade"], "B", "one grade below the A channel")
        capped = await YouTubeConnector(api_key="k", active=True, client=client).run({"target": channel_id, "hops": 2}, self.context())
        self.assertEqual(capped.leads, [], "the hop cap holds")
        unverified = self.channel("https://www.youtube.com/@bgscet_fan")
        loose = await YouTubeConnector(api_key="k", active=True, client=api({CHANNELS: (200, channel_body("UC" + "c" * 22, description=description, uploads=False))})).run({"target": unverified, "hops": 0}, self.context())
        self.assertEqual(loose.leads, [], "an unverified channel vouches for nothing")


RSS_EN = """<?xml version="1.0"?><rss version="2.0"><channel><title>The Hindu - Karnataka</title>
<item><title>Students of BGS College of Engineering and Technology win a national hackathon</title><link>https://news.example/en/1</link>
<description>&lt;p&gt;Bengaluru: a team from BGS College of Engineering and Technology won the finals.&lt;/p&gt;</description><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>BGS warns of landslides after heavy rain</title><link>https://news.example/en/2</link>
<description>The British Geological Survey (BGS) said slopes in the Highlands are unstable.</description><pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Parents file complaint against BGS College of Engineering and Technology over fees</title><link>https://news.example/en/3</link>
<description>Bengaluru: the police registered the complaint; the matter is before the High Court.</description><pubDate>Sun, 20 Sep 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""
RSS_KN = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Prajavani</title>
<item><title>{KANNADA_NAME} ವಿದ್ಯಾರ್ಥಿಗಳ ಸಾಧನೆ</title><link>https://kn-news.example/1</link>
<description>ಬೆಂಗಳೂರು: {KANNADA_NAME} ವಿದ್ಯಾರ್ಥಿಗಳು ರಾಷ್ಟ್ರಮಟ್ಟದ ಸ್ಪರ್ಧೆಯಲ್ಲಿ ಗೆದ್ದರು.</description><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>{KANNADA_NAME} ವಿರುದ್ಧ ಹೈಕೋರ್ಟ್‌ನಲ್ಲಿ ಅರ್ಜಿ</title><link>https://kn-news.example/2</link>
<description>ಬೆಂಗಳೂರು: {KANNADA_NAME} ಶುಲ್ಕ ಹೆಚ್ಚಳದ ವಿರುದ್ಧ ಪೋಷಕರು ನ್ಯಾಯಾಲಯದ ಮೊರೆ ಹೋದರು.</description><pubDate>Mon, 21 Sep 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""
EN_FEED, KN_FEED = "https://news.example/rss", "https://kn-news.example/rss"


class NewsFeedTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store.upsert_entity(INSTITUTION, name=PROFILE_NAME, kind="institution", names=[KANNADA_NAME])
        self.feeds = (NewsFeed("The Hindu (Karnataka)", "en", EN_FEED), NewsFeed("Prajavani", "kn", KN_FEED))

    def test_the_seeded_publishers_are_real_https_feeds_in_english_and_kannada(self):
        feeds = load_news_feeds()
        self.assertGreaterEqual(len(feeds), 5)
        self.assertEqual({feed.language for feed in feeds}, {"en", "kn"})
        self.assertTrue(all(feed.url.startswith("https://") for feed in feeds))
        connector = NewsFeedConnector(active=True)
        planned = connector.plan(self.context())
        self.assertEqual([lead.target for lead in planned], [feed.url for feed in feeds], "one source per seeded feed")
        self.assertEqual({(lead.interval_seconds, lead.origin) for lead in planned}, {(86400, "recurring")})

    async def test_english_and_kannada_items_are_mentions_and_only_court_news_goes_to_review(self):
        site = Site({EN_FEED: (200, RSS_EN, "application/rss+xml"), KN_FEED: (200, RSS_KN, "application/rss+xml; charset=utf-8")}, etags=True)
        fetcher = site.fetcher()
        connector = NewsFeedConnector(active=True, feeds=self.feeds)
        assets, evidence = len(self.store.list_assets(INSTITUTION)), len(self.store.list_evidence(INSTITUTION))
        english = await connector.run({"target": EN_FEED}, self.context(fetcher=fetcher))
        mentioned = [note for note in english.notes if note.startswith("mention")]
        self.assertEqual(len(mentioned), 2, "the hackathon and the complaint; the British Geological Survey is not BGS College")
        self.assertFalse(any("landslide" in note for note in english.notes))
        self.assertEqual([(item["kind"], item["url"]) for item in english.review], [("news_mention", "https://news.example/en/3")])
        self.assertEqual(english.mentions, 1, "the rest are counted for the digest")
        kannada = await connector.run({"target": KN_FEED}, self.context(fetcher=fetcher))
        self.assertEqual([item["url"] for item in kannada.review], ["https://kn-news.example/2"], "a Kannada item about a court case")
        self.assertEqual(kannada.mentions, 1)
        self.assertEqual((len(self.store.list_assets(INSTITUTION)), len(self.store.list_evidence(INSTITUTION))), (assets, evidence), "a mention is never evidence")
        again = await connector.run({"target": EN_FEED}, self.context(fetcher=fetcher))
        self.assertEqual((again.outcome, again.mentions), ("not_modified", 0), "reads are conditional")
        reread = await connector.run({"target": EN_FEED}, self.context(fetcher=Site({EN_FEED: (200, RSS_EN, "application/rss+xml")}).fetcher()))
        self.assertEqual(reread.mentions, 0, "items already read are not counted twice")

    async def test_a_tick_counts_mentions_for_the_digest(self):
        site = Site({EN_FEED: (200, RSS_EN, "application/rss+xml")})
        engine = MapEngine(self.store, ConnectorRegistry([NewsFeedConnector(active=True, feeds=self.feeds[:1])]), fetcher=site.fetcher(), clock=lambda: NOW)
        result = await engine.tick(INSTITUTION)
        self.assertEqual((result["counts"]["mentions"], result["counts"]["review"]), (1, 1))
        digest = IncidentDesk(self.store).digest(INSTITUTION)
        self.assertEqual(digest["mentions"], 1)
        self.assertIn("News: 1 item(s)", digest["summary"])
        self.assertEqual(digest["review_waiting"], {"news_mention": 1})

    def test_a_manager_adds_a_news_feed_polled_at_its_own_pace(self):
        engine = MapEngine(self.store, map_connectors(AppSettings()), clock=lambda: NOW)
        service = MapService(self.store, engine=engine)
        manager = principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION)
        news = service.add_source(manager, INSTITUTION, connector="news_feed", target="https://news.example/other.rss")
        listing = service.add_source(manager, INSTITUTION, connector="directory", target="https://www.nirfindia.org/2026/engineering")
        self.assertEqual(self.store.get_source(INSTITUTION, news["source_id"])["interval_seconds"], 86400)
        self.assertEqual(self.store.get_source(INSTITUTION, listing["source_id"])["interval_seconds"], 30 * 86400)


PROFILE_NAME = "BGS College of Engineering and Technology"


class WatchScopeTests(Fixture):
    def community_domain(self):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://bgscet-admissions.com/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="community_record", detail="wikidata:Q1:P856", channel="wikidata", observed_via="index")
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="community_record", detail="osm:way/1:website", channel="openstreetmap", observed_via="index")
        regrade(self.store, INSTITUTION, [asset_id])
        return asset_id

    def test_rdap_and_dns_watch_only_named_or_well_graded_domains(self):
        community = self.community_domain()
        self.assertEqual(self.store.get_asset(INSTITUTION, community)["grade"], "C", "Wikidata and OpenStreetMap together are still one community channel")
        for connector in (RdapConnector(active=True), DnsConnector(active=True)):
            self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.domain_id], f"{connector.name} ignores a domain only community edits call official")
        self.store.add_evidence(INSTITUTION, asset_id=community, kind="reviewer_confirm", detail="it is ours", channel="reviewer:x", observed_via="reviewer")
        self.assertIn(community, [lead.target for lead in RdapConnector(active=True).plan(self.context())], "a reviewer's confirmation makes it watched")

    def test_community_records_are_one_channel_family(self):
        def ev(kind, channel):
            return {"kind": kind, "polarity": "supports", "observed_via": "index", "detail": "", "channel": channel, "observed_at": NOW.isoformat(), "source_url": "https://x.example/"}

        account = {"kind": "account"}
        self.assertEqual(grade(account, [ev("community_record", "wikidata"), ev("community_record", "openstreetmap")]).grade, "C")
        self.assertEqual(grade(account, [ev("community_record", "wikidata"), ev("community_record", "directory:unstop.com")]).grade, "C")
        self.assertEqual(grade(account, [ev("community_record", "wikidata"), ev("community_record", "openstreetmap"), ev("search_snippet", "search:tavily")]).grade, "B")


class EscalationTests(Fixture):
    def rdap(self, expiry):
        return RdapConnector(active=True, client=api({("rdap.org", "/domain/bgscet.ac.in"): (200, {"events": [{"eventAction": "expiration", "eventDate": expiry.isoformat()}], "status": ["active"]})}))

    async def test_an_expiring_domain_becomes_urgent_two_weeks_out_even_after_the_digest(self):
        expiry = NOW + timedelta(days=40)
        sent = []
        desk = IncidentDesk(self.store, recipients=lambda institution_id: ("manager@bgscet.ac.in",), notify=lambda institution_id, recipients, title, body, incident_id: sent.append(title))
        early = await self.rdap(expiry).run({"target": self.domain_id}, self.context())
        self.assertEqual(early.incidents[0]["severity"], "medium")
        self.assertEqual(desk.record(INSTITUTION, early.incidents)["notified"], 0, "medium waits for the digest")
        desk.send_digest(INSTITUTION)
        [incident] = self.store.list_incidents(INSTITUTION)
        self.store.set_incident_status(INSTITUTION, incident["incident_id"], status="acknowledged", by="manager")
        late = await self.rdap(expiry).run({"target": self.domain_id}, self.context(now=NOW + timedelta(days=27)))
        self.assertEqual(late.incidents[0]["severity"], "high", "13 days left")
        counts = desk.record(INSTITUTION, late.incidents)
        self.assertEqual((counts["escalated"], counts["notified"]), (1, 1))
        self.assertTrue(sent[-1].startswith("Urgent: "))
        [incident] = self.store.list_incidents(INSTITUTION)
        self.assertEqual((incident["severity"], incident["status"]), ("high", "open"), "an acknowledged warning is open again once urgent")
        self.assertEqual(desk.record(INSTITUTION, late.incidents)["notified"], 0, "and alerted once")

    def test_the_store_reports_an_escalation(self):
        _, first = self.store.upsert_incident(INSTITUTION, kind="domain_expiring", target="bgscet.ac.in", severity="medium", title="x")
        self.store.mark_incidents_notified(INSTITUTION, [row["incident_id"] for row in self.store.list_incidents(INSTITUTION)])
        row, verdict = self.store.upsert_incident(INSTITUTION, kind="domain_expiring", target="bgscet.ac.in", severity="high", title="x")
        self.assertEqual((first, verdict, row["severity"], row["notified_at"]), ("new", "escalated", "high", None))
        row, verdict = self.store.upsert_incident(INSTITUTION, kind="domain_expiring", target="bgscet.ac.in", severity="medium", title="x")
        self.assertEqual((verdict, row["severity"]), ("repeat", "high"), "severity never drops on its own")


class LookalikeDomainTests(Fixture):
    def client(self, resolving=(), certified=()):
        def dns(request):
            return (200, {"Status": 0, "Answer": [{"data": "203.0.113.7"}]}) if request.url.params["name"] in resolving else (200, {"Status": 3})

        def certs(request):
            name = request.url.params["domain"]
            return 200, ([{"id": "9", "dns_names": [name, f"www.{name}"]}] if name in certified else [])

        return api({("dns.google", "/resolve"): dns, ("api.certspotter.com", "/v1/issuances"): certs})

    def test_variants_cover_the_usual_slips(self):
        names = variants("college.ac.in")
        for expected in ("colege.ac.in", "ocllege.ac.in", "ccollege.ac.in", "c0llege.ac.in", "co1lege.ac.in", "col-lege.ac.in", "college.com", "college.edu.in", "college.in"):
            self.assertIn(expected, names)
        self.assertIn("msit.ac.in", variants("rnsit.ac.in"), "rn read as m")
        self.assertIn("brnsce.ac.in", variants("bmsce.ac.in"), "m read as rn")
        self.assertTrue(any(name.startswith("xn--") for name in names), "Cyrillic homoglyphs, as IDN")
        self.assertNotIn("college.ac.in", names)

    async def test_registered_lookalikes_go_to_review_and_nothing_else(self):
        client = self.client(resolving={"bgscet.com", "bgsct.ac.in", "xn--bgset-3ye.ac.in"}, certified={"bgscet.org"})
        connector = LookalikeDomainConnector(active=True, client=client, token="cs-key")
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.domain_id], "the configured domain")
        before = len(self.store.list_assets(INSTITUTION))
        result = await connector.run({"target": self.domain_id, "runs": 0}, self.context())
        self.assertEqual({item["url"] for item in result.review}, {"https://bgscet.com/", "https://bgsct.ac.in/", "https://xn--bgset-3ye.ac.in/", "https://bgscet.org/"})
        self.assertEqual({item["kind"] for item in result.review}, {"lookalike_domain"})
        self.assertIn("bgsсet.ac.in looks like bgscet.ac.in and is registered", {item["title"] for item in result.review}, "an IDN is shown as people read it")
        self.assertEqual((result.leads, result.new_assets, len(self.store.list_assets(INSTITUTION))), ([], [], before), "no leads and no assets")
        dns = [request for request in client.seen if request.url.host == "dns.google"]
        certs = [request for request in client.seen if request.url.host == "api.certspotter.com"]
        self.assertLessEqual(len(dns), 60)
        self.assertEqual(sorted(request.url.params["domain"] for request in certs), sorted(f"bgscet.{suffix}" for suffix in ("in", "co.in", "org", "com", "net", "edu.in")))
        self.assertEqual({request.headers["authorization"] for request in certs}, {"Bearer cs-key"})
        self.assertEqual(result.cost, float(len(dns) + len(certs)))

    async def test_each_run_checks_at_most_the_cap_and_carries_on(self):
        client = self.client()
        connector = LookalikeDomainConnector(active=True, client=client, max_variants=10)
        self.store.upsert_asset(INSTITUTION, asset_ref("https://bgscet.com/"), entity_id=None, relation="unknown")
        await connector.run({"target": self.domain_id, "runs": 0}, self.context())
        first = [request.url.params["name"] for request in client.seen if request.url.host == "dns.google"]
        await connector.run({"target": self.domain_id, "runs": 1}, self.context())
        second = [request.url.params["name"] for request in client.seen if request.url.host == "dns.google"][len(first):]
        self.assertEqual((len(first), len(second)), (10, 10))
        self.assertFalse(set(first) & set(second), "the next run carries on where this one stopped")
        self.assertNotIn("bgscet.com", first + second, "a name already in the map is not news")


UNSTOP = "https://unstop.com/hackathons/advaya-bgs-college-of-engineering-and-technology-bengaluru-karnataka-1443757"
DEVPOST = "https://advaya.devpost.com/"
GDG = "https://gdg.community.dev/gdg-on-campus-bgs-college-of-engineering-and-technology/"


class DirectoryTests(Fixture):
    def page(self, url):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation="official")
        return asset_id

    async def test_seeded_directory_pages_are_scheduled_and_rechecked(self):
        pages = {self.store.get_asset(INSTITUTION, asset_id)["url"]: asset_id for asset_id in (self.page(url) for url in (UNSTOP, DEVPOST, GDG))}
        self.page("https://ieeebangalore.org/student-branches/")
        connector = DirectoryConnector(active=True)
        planned = {lead.target: lead for lead in connector.plan(self.context())}
        self.assertEqual(set(planned), set(pages), "unstop, devpost and GDG pages; an ordinary site's page is left to recheck")
        self.assertEqual({(lead.work_class, lead.origin) for lead in planned.values()}, {("recheck", "recurring")})
        self.assertEqual({url: lead.asset_id for url, lead in planned.items()}, pages)
        site = Site({UNSTOP: (200, "<html><head><title>Advaya hackathon | BGS College of Engineering and Technology</title></head><body><main>Register</main></body></html>")})
        source = {"target": UNSTOP, "asset_id": pages[UNSTOP], "origin": "recurring"}
        await connector.run(source, self.context(fetcher=site.fetcher()))
        self.assertEqual([(row["kind"], row["polarity"]) for row in self.store.list_evidence(INSTITUTION, asset_id=pages[UNSTOP])], [("liveness", "supports")])
        self.assertEqual(self.store.get_asset(INSTITUTION, pages[UNSTOP])["status"], "live")
        await connector.run({"target": DEVPOST, "asset_id": pages[DEVPOST], "origin": "recurring"}, self.context(fetcher=Site({}).fetcher()))
        self.assertEqual(self.store.list_evidence(INSTITUTION, asset_id=pages[DEVPOST])[-1]["polarity"], "refutes")
        for lead in planned.values():
            self.store.upsert_source(INSTITUTION, connector=lead.connector, target=lead.target, asset_id=lead.asset_id, origin=lead.origin, work_class=lead.work_class)
        self.assertEqual(connector.plan(self.context()), [], "planned once")

    def test_only_the_exact_regulator_hosts_are_authorities(self):
        self.assertEqual(authority_of("https://facilities.aicte-india.org/dashboard/pages/angulardashboard.php"), "aicte")
        self.assertEqual(authority_of("https://assessmentonline.naac.gov.in/public/index.php/hei_dashboard"), "naac")
        self.assertEqual(authority_of("https://vtu.ac.in/en/affiliated-colleges/"), "vtu")
        for other in ("https://student-portal.vtu.ac.in/", "https://blog.nirfindia.org/", "https://anything.naac.gov.in/", "https://nirfindia.org.evil.example/"):
            self.assertIsNone(authority_of(other), other)

    async def test_a_regulators_first_word_on_an_unknown_domain_waits_for_a_person(self):
        listing = NIRF.replace('<tr><td>Other College</td>', '<tr><td>BGS College of Engineering and Technology (new site)</td><td><a href="https://bgscet.edu.in/">bgscet.edu.in</a></td></tr><tr><td>Other College</td>')
        site = Site({"https://www.nirfindia.org/2026/engineering": (200, listing)})
        connector = DirectoryConnector(active=True)
        result = await connector.run({"target": "https://www.nirfindia.org/2026/engineering"}, self.context(fetcher=site.fetcher()))
        new = self.store.find_asset(INSTITUTION, "web:bgscet.edu.in")
        self.assertEqual([(item["kind"], item["asset_id"]) for item in result.review], [("authority_nomination", new["asset_id"])], "the configured domain needs no review")
        regrade(self.store, INSTITUTION, [new["asset_id"]])
        new = self.store.get_asset(INSTITUTION, new["asset_id"])
        self.assertEqual((new["grade"], new["relation"], nominated(self.store, INSTITUTION, new["asset_id"])), ("C", "unknown", False), "not an anchor yet")
        self.assertTrue({"confirm", "reject"} <= DECISIONS["authority_nomination"])
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="authority_nomination", title="x", asset_id=new["asset_id"])
        MapService(self.store).decide(principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION), INSTITUTION, review_id, decision="confirm", relation="official")
        await connector.run({"target": "https://www.nirfindia.org/2026/engineering"}, self.context(fetcher=site.fetcher()))
        regrade(self.store, INSTITUTION, [new["asset_id"]])
        self.assertEqual(self.store.get_asset(INSTITUTION, new["asset_id"])["grade"], "A", "confirmed once, the regulator's listing anchors it")


CDX_HOSTS = [["original"], ["http://www.bgscet.ac.in/"], ["https://bgscet.ac.in/about"], ["http://admissions.bgscet.ac.in/apply.php"], ["https://library.bgscet.ac.in:443/"], ["http://bgscet.ac.in.evil.example/"], ["https://alumni.bgscet.ac.in/x"], ["http://ADMISSIONS.bgscet.ac.in/fees"]]
CONTACT_PAGE = "<html><head><title>Contact us</title></head><body><main><p>Write to the math office.</p></main><footer><a href='https://www.instagram.com/adichunchanagiri_math/'>Instagram</a></footer></body></html>"


class WaybackTests(Fixture):
    async def test_the_archive_lists_hosts_under_a_named_domain(self):
        self.store.upsert_asset(INSTITUTION, asset_ref("https://alumni.bgscet.ac.in/"), entity_id=self.entity_id, relation="official")
        client = api({("web.archive.org", "/cdx/search/cdx"): (200, CDX_HOSTS)})
        connector = WaybackConnector(active=True, client=client)
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.domain_id + HOSTS_SUFFIX], "a healthy named domain: hosts only")
        self.assertEqual(connector.cost({"target": self.domain_id + HOSTS_SUFFIX}), 1.0)
        result = await connector.run({"target": self.domain_id + HOSTS_SUFFIX, "hops": 0}, self.context())
        params = client.seen[-1].url.params
        self.assertEqual({key: params[key] for key in ("url", "matchType", "fl", "collapse", "filter", "limit")}, {"url": "bgscet.ac.in", "matchType": "domain", "fl": "original", "collapse": "urlkey", "filter": "statuscode:200", "limit": "500"})
        self.assertEqual([(lead.connector, lead.target, lead.hops) for lead in result.leads], [("lead_page", "https://admissions.bgscet.ac.in/", 1), ("lead_page", "https://library.bgscet.ac.in/", 1)])

    async def test_a_lapsed_sites_archived_contact_page_is_read_too(self):
        math = InstitutionProfile(INSTITUTION, "Sri Adichunchanagiri Mahasamsthana Math", "Nagamangala", official_domains=["acmbgs.org"])
        domain_id = sync_profile(self.store, math)["domains_added"][0]
        self.store.add_evidence(INSTITUTION, asset_id=domain_id, kind="integrity", polarity="refutes", detail="parked:parked_lander", source_url="https://acmbgs.org/", channel="site:acmbgs.org")
        regrade(self.store, INSTITUTION, [domain_id])
        home = OLD_HOME.replace("<p>Welcome</p>", '<p>Welcome</p><nav><a href="/contact-us/">Contact us</a></nav>')
        routes = {
            ("web.archive.org", "/cdx/search/cdx"): (200, [["timestamp", "original", "statuscode", "mimetype"], ["20190101000000", "https://acmbgs.org/", "200", "text/html"]]),
            ("web.archive.org", "/web/20190101000000id_/https://acmbgs.org/"): (200, home),
            ("web.archive.org", "/web/20190101000000id_/https://acmbgs.org/contact-us/"): (200, CONTACT_PAGE),
        }
        result = await WaybackConnector(active=True, client=api(routes)).run({"target": domain_id}, self.context())
        self.assertIn("instagram:adichunchanagiri_math", result.new_assets)
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("instagram:adichunchanagiri_math"), "A-arch")


LOOKUP = ("nominatim.openstreetmap.org", "/lookup")


class OpenStreetMapPolicyTests(Fixture):
    async def test_a_chosen_place_is_re_read_by_its_id(self):
        self.assertGreaterEqual(OpenStreetMapConnector().min_gap_seconds, 15.0, "well clear of Nominatim's one request a second")
        client = api({NOMINATIM: (200, [PLACE]), LOOKUP: (200, [PLACE])})
        connector = OpenStreetMapConnector(active=True, client=client, min_gap_seconds=0)
        first = await connector.run({"target": self.entity_id}, self.context())
        self.assertEqual((client.seen[-1].url.path, first.etag), ("/search", "osm:way/123456"), "the chosen object is kept on the source")
        second = await connector.run({"target": self.entity_id, "etag": first.etag}, self.context())
        self.assertEqual((client.seen[-1].url.path, client.seen[-1].url.params["osm_ids"], second.outcome), ("/lookup", "W123456", "ok"))
        gone = await OpenStreetMapConnector(active=True, client=api({LOOKUP: (200, [])}), min_gap_seconds=0).run({"target": self.entity_id, "etag": first.etag}, self.context())
        self.assertEqual((gone.outcome, gone.etag), ("place_changed", "osm:"), "searched for again next time")
        searched = api({NOMINATIM: (200, [PLACE])})
        await OpenStreetMapConnector(active=True, client=searched, min_gap_seconds=0).run({"target": self.entity_id, "etag": "osm:"}, self.context())
        self.assertEqual(searched.seen[-1].url.path, "/search")
        regrade(self.store, INSTITUTION)
        exported = {line.split("\t")[2]: line for line in export_tsv(self.store, INSTITUTION).splitlines()[1:]}
        self.assertIn("© OpenStreetMap contributors (ODbL)", exported["https://www.instagram.com/bgscet_engg_coll/"])
        self.assertNotIn("OpenStreetMap", exported["https://bgscet.ac.in/"], "a row not derived from OpenStreetMap carries no attribution")

    def test_the_connector_needs_a_crawler_contact(self):
        with self.assertRaisesRegex(ValueError, "CRAWLER_CONTACT"):
            AppSettings(intelligence_connectors=("openstreetmap",)).ensure_safe_for_production()
        try:
            AppSettings(intelligence_connectors=("openstreetmap",), intelligence_crawler_contact="https://bgscet.ac.in/crawler").ensure_safe_for_production()
        except ValueError as exc:  # other production settings may still be missing in a test environment
            self.assertNotIn("CRAWLER_CONTACT", str(exc))


class GooglePlayTests(Fixture):
    async def test_a_listing_over_a_megabyte_is_read(self):
        app_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://play.google.com/store/apps/details?id=in.ac.bgscet.connect"), entity_id=self.entity_id, relation="official")
        big = LISTING.replace("</body>", "<div>" + "x" * 1_300_000 + "</div></body>")
        result = await GooglePlayConnector(active=True).run({"target": app_id}, self.context(fetcher=Site({"https://play.google.com/store/apps/details": (200, big)}).fetcher()))
        self.assertEqual((result.outcome, result.failed), ("ok", False))
        self.assertIn("backlink", {row["kind"] for row in self.store.list_evidence(INSTITUTION, asset_id=app_id)})

    async def test_apps_are_found_through_the_search_index_by_their_developer(self):
        hits = (
            SearchHit(url="https://play.google.com/store/apps/dev?id=555", title="Android Apps by BGS College of Engineering and Technology on Google Play", snippet="Apps for BGS College of Engineering and Technology."),
            SearchHit(url="https://play.google.com/store/apps/details?id=in.ac.bgscet.connect", title="BGSCET Connect - Apps on Google Play", snippet="Offered by BGSCET Official. Campus news for BGS College of Engineering and Technology."),
            SearchHit(url="https://play.google.com/store/apps/details?id=com.bgsgroup.app", title="BGS Connect - Apps on Google Play", snippet="Offered by BGS Group of Institutions. Also for BGS College of Engineering and Technology."),
            SearchHit(url="https://play.google.com/store/apps/details?id=com.fan.bgscet", title="BGSCET Notes - Apps on Google Play", snippet="Offered by Rahul Kumar. Notes for BGS College of Engineering and Technology."),
            SearchHit(url="https://play.google.com/store/search?q=BGS%20College&c=apps", title="BGS College of Engineering and Technology - Android Apps by BGS College of Engineering and Technology on Google Play", snippet="search"),
        )
        search = StaticSearchProvider(hits)
        connector = GooglePlaySearchConnector(active=True)
        self.assertEqual([lead.target for lead in connector.plan(self.context(search=search))], [self.entity_id], "look-alikes are not searched for")
        result = await connector.run({"target": self.entity_id}, self.context(search=search))
        self.assertIn("site:play.google.com/store/apps", search.calls[-1])
        self.assertEqual(sorted(result.new_assets), ["google_play:app:in.ac.bgscet.connect", "google_play:dev:555"], "the developer is the college; never Play's own search page")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("google_play:app:in.ac.bgscet.connect"), "C", "one channel until the listing links back")


class WiringTests(unittest.TestCase):
    def test_the_new_connectors_are_off_until_named(self):
        self.assertFalse({"news_feed", "lookalike_domains", "google_play_search"} & set(map_connectors(AppSettings()).names()))
        on = map_connectors(AppSettings(intelligence_connectors=("news_feed", "lookalike_domains", "google_play_search", "certificates"), intelligence_certspotter_token="cs"))
        described = {item["name"]: item for item in on.describe()}
        self.assertEqual({name: described[name]["budget_key"] for name in ("news_feed", "lookalike_domains", "google_play_search", "certificates")}, {"news_feed": "feed", "lookalike_domains": "lookalikes", "google_play_search": "search", "certificates": "certificates"})
        self.assertEqual((described["news_feed"]["interval_seconds"], described["news_feed"]["max_grade"], described["news_feed"]["access_mode"]), (86400, "C", "public_feed"))
        self.assertEqual(described["lookalike_domains"]["access_mode"], "registry")
        self.assertIn("certificates", AppSettings().intelligence_budget_caps())
        with self.assertRaisesRegex(ValueError, "SEARCH_PROVIDER"):
            AppSettings(intelligence_connectors=("google_play_search",)).ensure_safe_for_production()
        added = {"news_mention", "lookalike_domain", "authority_nomination", "handle_reassigned"}
        self.assertTrue(added <= REVIEW_KINDS and added <= set(DECISIONS), "each new kind can be queued and decided")

    def test_a_manually_added_news_feed_is_allowed(self):
        from app.internet_intelligence.map.service import MANUAL_SOURCE_CONNECTORS

        self.assertIn("news_feed", MANUAL_SOURCE_CONNECTORS)


if __name__ == "__main__":
    unittest.main()
