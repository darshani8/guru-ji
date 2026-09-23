import importlib.util
import json
import unittest
from uuid import uuid4
from datetime import timedelta
from unittest import mock

import httpx

from app.config.settings import AppSettings
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import CourtRecordsConnector, WikidataConnector, YouTubeConnector
from app.internet_intelligence.map.connectors.archive import WaybackConnector
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry
from app.internet_intelligence.map.connectors.common import ApiClient, names_entity, parse_feed
from app.internet_intelligence.map.connectors.feeds import FeedConnector, youtube_feed_url
from app.internet_intelligence.map.connectors.hubs import DirectoryConnector, LinkHubConnector, authority_of
from app.internet_intelligence.map.connectors.infrastructure import CertificateConnector, DnsConnector, RdapConnector, registrable
from app.internet_intelligence.map.connectors.search import PLATFORM_DOMAINS, SearchConnector, SpamProbeConnector
from app.internet_intelligence.map.connectors.web import LeadPageConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.search import SearchHit, StaticSearchProvider
from test_intelligence_map_engine import HOME, NOW, PROFILE, Site

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

INSTITUTION = "bgscet"


class _RecordingClient(ApiClient):
    """An ApiClient that also remembers the requests it sent."""


def api(routes):
    """An ApiClient whose requests are answered by ``routes[(host, path)]`` (a response or a callable)."""

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = routes.get((request.url.host, request.url.path))
        if callable(answer):
            answer = answer(request)
        if answer is None:
            return httpx.Response(404, request=request)
        status, body = answer
        content = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return httpx.Response(status, content=content, request=request)

    client = _RecordingClient(transport=httpx.MockTransport(handler))
    client.seen = seen
    return client


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        synced = sync_profile(self.store, PROFILE)
        self.entity_id = synced["entity_id"]
        self.domain_id = synced["domains_added"][0]
        regrade(self.store, INSTITUTION, [self.domain_id])
        self.store.upsert_entity(INSTITUTION, name="BGS Group of Institutions", kind="lookalike")

    def context(self, *, fetcher=None, search=None, now=NOW):
        return ConnectorContext(self.store, INSTITUTION, "run-1", now, fetcher=fetcher, search=search)

    def grade_of(self, key):
        return self.store.find_asset(INSTITUTION, key)["grade"]


class HelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_api_client_reports_refusals_and_refuses_downgrades(self):
        client = api({("api.example", "/limited"): (429, ""), ("api.example", "/ok"): (200, {"a": 1}), ("api.example", "/down"): (503, "")})
        self.assertEqual((await client.request("https://api.example/limited")).outcome, "blocked")
        self.assertEqual((await client.request("https://api.example/down")).outcome, "server_error")
        self.assertEqual((await client.request("https://api.example/ok")).json(), {"a": 1})
        with self.assertRaises(ValueError):
            await client.request("http://api.example/ok")

        def to_http(request):
            return httpx.Response(302, headers={"location": "http://api.example/ok"}, request=request)

        downgrade = ApiClient(transport=httpx.MockTransport(to_http))
        self.assertEqual((await downgrade.request("https://api.example/x")).outcome, "error", "a redirect to plain HTTP is not followed")
        small = ApiClient(max_bytes=10, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 100, request=request)))
        self.assertEqual((await small.request("https://api.example/big")).outcome, "too_large")

    def test_feeds_parse_and_declarations_are_refused(self):
        rss = b"<?xml version='1.0'?><rss><channel><title>News</title><item><title>Fest</title><link>https://bgscet.ac.in/fest</link><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"
        title, items = parse_feed(rss)
        self.assertEqual((title, items[0].link, items[0].published_at.year), ("News", "https://bgscet.ac.in/fest", 2026))
        atom = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Channel</title><entry><title>Video</title><link rel='alternate' href='https://www.youtube.com/watch?v=abc'/><published>2026-09-01T00:00:00+00:00</published></entry></feed>"
        self.assertEqual(parse_feed(atom)[1][0].link, "https://www.youtube.com/watch?v=abc")
        bomb = b"<!-- padding -->" * 1000 + b"<!DOCTYPE lolz [<!ENTITY lol 'lol'>]><rss><channel><title>&lol;</title></channel></rss>"
        self.assertIsNone(parse_feed(bomb), "a document with declarations is refused wherever they appear")
        self.assertIsNone(parse_feed(b"<html><body>not a feed</body></html>"))
        self.assertIsNone(parse_feed(b"not xml at all"))

    def test_an_account_must_carry_the_name_itself(self):
        entity = {"name": "BGS College of Engineering and Technology", "names": ["BGSCET", "AIT"]}
        self.assertTrue(names_entity(entity, handle="bgscet_engg_coll", title="Anything"))
        self.assertTrue(names_entity(entity, handle="x", title="BGS College of Engineering and Technology (@x) • Instagram photos"))
        self.assertFalse(names_entity(entity, handle="rahul.k", title="Rahul Kumar (@rahul.k) • Instagram photos and videos"), "a bio mention is not enough")
        self.assertTrue(names_entity(entity, handle="ait", title=""), "a short acronym counts as the whole handle")
        self.assertFalse(names_entity(entity, handle="aitech_robotics", title=""), "but not inside a longer one")

    def test_small_lookups(self):
        self.assertEqual(registrable("www.alumni.bgscet.ac.in"), "bgscet.ac.in")
        self.assertEqual(registrable("sub.acmbgs.org"), "acmbgs.org")
        self.assertEqual(authority_of("https://www.nirfindia.org/2026/EngineeringRanking.html"), "nirf")
        self.assertIsNone(authority_of("https://nirfindia.org.evil.example/"))
        self.assertEqual(len(PLATFORM_DOMAINS), 10)


class GraderAdditionsTests(unittest.TestCase):
    def test_community_records_and_subdomains(self):
        account = {"kind": "account"}

        def ev(kind, channel, detail="", via="index"):
            return {"kind": kind, "polarity": "supports", "observed_via": via, "detail": detail, "channel": channel, "observed_at": NOW.isoformat(), "source_url": "https://x.example/"}

        self.assertEqual(grade(account, [ev("community_record", "wikidata")]).grade, "C")
        self.assertEqual(grade(account, [ev("search_snippet", "search:tavily"), ev("search_snippet", "search:brave")]).grade, "B", "two independent channels")
        self.assertEqual(grade(account, [ev("community_record", "wikidata"), ev("search_snippet", "search:tavily")]).grade, "B")
        self.assertEqual(grade(account, [ev("search_snippet", "search:tavily"), ev("search_snippet", "search:tavily")]).grade, "C", "one channel twice is still one")
        self.assertEqual(grade({"kind": "domain"}, [ev("subdomain", "dns", "A:bgscet.ac.in", via="live")]).grade, "B")
        self.assertEqual(grade({"kind": "domain"}, [ev("subdomain", "dns", "A:bgscet.ac.in", via="index")]).grade, "unrated", "only a live subdomain counts")


class SearchConnectorTests(Fixture):
    HITS = (
        SearchHit(url="https://www.instagram.com/bgscet_engg_coll/", title="BGSCET (@bgscet_engg_coll) • Instagram photos and videos", snippet="BGS College of Engineering and Technology, Mahalakshmipuram, Bengaluru."),
        SearchHit(url="https://www.instagram.com/rahul.k/", title="Rahul Kumar (@rahul.k) • Instagram photos and videos", snippet="Student at BGS College of Engineering and Technology, Bengaluru."),
        SearchHit(url="https://www.instagram.com/bgsgroup/", title="BGS Group of Institutions (@bgsgroup)", snippet="BGS Group of Institutions, BGS College of Engineering and Technology"),
        SearchHit(url="https://www.instagram.com/bgscet.official/", title="BGSCET Official (@bgscet.official) • Instagram", snippet="Official page of BGS College of Engineering and Technology, Bengaluru"),
        SearchHit(url="https://www.instagram.com/p/Cxyz/", title="BGSCET fest post", snippet="BGS College of Engineering and Technology fest"),
    )

    async def test_accounts_found_by_search_are_single_channel_candidates(self):
        search = StaticSearchProvider(self.HITS)
        connector = SearchConnector(active=True)
        planned = connector.plan(self.context(search=search))
        self.assertEqual(len([lead for lead in planned if lead.work_class == "rotation"]), len(PLATFORM_DOMAINS), "one source per platform for the one mapped entity (never the look-alike)")
        self.assertEqual([lead.target.split("|")[1] for lead in planned if lead.work_class == "explore"], ["*"], "and one open-web search for new leads")
        self.assertEqual(connector.plan(self.context(search=None)), [], "no provider, no plan")
        for lead in planned:
            self.store.upsert_source(INSTITUTION, connector=lead.connector, target=lead.target, entity_id=lead.entity_id, origin=lead.origin, work_class=lead.work_class)
        self.assertEqual(connector.plan(self.context(search=search)), [], "planning is idempotent")
        result = await connector.run({"target": f"{self.entity_id}|instagram.com"}, self.context(search=search))
        self.assertEqual(search.calls[-1], '"BGS College of Engineering and Technology" Bengaluru')
        self.assertEqual(sorted(result.new_assets), ["instagram:bgscet.official", "instagram:bgscet_engg_coll"])
        self.assertIsNone(self.store.find_asset(INSTITUTION, "instagram:rahul.k"), "a person who mentions the college is not mapped")
        self.assertIsNone(self.store.find_asset(INSTITUTION, "instagram:bgsgroup"), "a look-alike named as strongly is not ours")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "C")
        self.assertEqual(result.review, [], "nothing to compare an 'official' claim against yet")

    async def test_a_second_official_account_is_sent_for_review(self):
        official = asset_ref("https://www.instagram.com/bgscet_engg_coll/")
        asset_id, _ = self.store.upsert_asset(INSTITUTION, official, entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=self.domain_id, channel="site:bgscet.ac.in")
        regrade(self.store, INSTITUTION, [asset_id])
        result = await SearchConnector(active=True).run({"target": f"{self.entity_id}|instagram.com"}, self.context(search=StaticSearchProvider(self.HITS)))
        self.assertEqual([item["kind"] for item in result.review], ["impersonation_candidate"])
        self.assertIn("bgscet.official", result.review[0]["title"])

    async def test_the_spam_probe_reports_cloaked_spam_without_regrading(self):
        hits = (
            SearchHit(url="https://bgscet.ac.in/wp-content/slot-gacor-maxwin.html", title="Situs Slot Gacor Maxwin", snippet="daftar slot online"),
            SearchHit(url="https://bgscet.ac.in/admissions/", title="Admissions", snippet="Apply for admission"),
            SearchHit(url="https://casino.example/bgscet", title="casino", snippet="casino"),
        )
        connector = SpamProbeConnector(active=True)
        self.assertEqual([lead.target for lead in connector.plan(self.context(search=StaticSearchProvider(hits)))], [self.domain_id])
        result = await connector.run({"target": self.domain_id}, self.context(search=StaticSearchProvider(hits)))
        self.assertEqual(result.outcome, "spam_found")
        self.assertEqual(result.incidents[0]["examples"], ["https://bgscet.ac.in/wp-content/slot-gacor-maxwin.html"])
        regrade(self.store, INSTITUTION, [self.domain_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, self.domain_id)["grade"], "A", "an indexed spam page is an incident, not a regrade")
        clean = await connector.run({"target": self.domain_id}, self.context(search=StaticSearchProvider(hits[1:2])))
        self.assertEqual((clean.outcome, clean.incidents), ("clean", []))


YT_CHANNEL = "UClACJA8pf4QbkdH4__7MTGA"
YT_FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>BGSCET</title>
<entry><title>Graduation day</title><link rel="alternate" href="https://www.youtube.com/watch?v=a1"/><published>2026-08-30T10:00:00+00:00</published></entry>
<entry><title>Fest</title><link rel="alternate" href="https://www.youtube.com/watch?v=a2"/><published>2026-03-01T10:00:00+00:00</published></entry></feed>"""


class FeedConnectorTests(Fixture):
    async def test_a_channel_feed_keeps_the_channel_alive_and_dated(self):
        channel_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(f"https://www.youtube.com/channel/{YT_CHANNEL}"), entity_id=self.entity_id, relation="official")
        connector = FeedConnector(active=True)
        [lead] = connector.plan(self.context())
        self.assertEqual((lead.target, lead.asset_id), (youtube_feed_url(YT_CHANNEL), channel_id))
        site = Site({youtube_feed_url(YT_CHANNEL).split("?")[0]: (200, YT_FEED, "application/atom+xml")}, etags=True)
        # The fake web keys pages by path; answer the feed whatever the query string.
        fetcher = site.fetcher()
        result = await connector.run({"target": lead.target, "asset_id": channel_id, "origin": "recurring"}, self.context(fetcher=fetcher))
        self.assertEqual(result.outcome, "ok")
        asset = self.store.get_asset(INSTITUTION, channel_id)
        self.assertEqual(asset["last_activity_at"][:10], "2026-08-30")
        self.assertEqual(self.store.list_evidence(INSTITUTION, asset_id=channel_id)[-1]["detail"], "ok:feed")
        again = await connector.run({"target": lead.target, "asset_id": channel_id, "origin": "recurring"}, self.context(fetcher=fetcher))
        self.assertEqual(again.outcome, "not_modified", "the second read is conditional")
        dormant = await connector.run({"target": lead.target, "asset_id": channel_id, "origin": "recurring"}, self.context(fetcher=Site({youtube_feed_url(YT_CHANNEL).split("?")[0]: (200, YT_FEED, "application/atom+xml")}).fetcher(), now=NOW + timedelta(days=800)))
        self.assertTrue(any(note.startswith("dormant") for note in dormant.notes))
        gone = await connector.run({"target": lead.target, "asset_id": channel_id, "origin": "recurring"}, self.context(fetcher=Site({}).fetcher()))
        self.assertEqual(gone.outcome, "not_found")
        self.assertEqual(self.store.list_evidence(INSTITUTION, asset_id=channel_id)[-1]["polarity"], "refutes")

    async def test_other_youtube_pages_and_non_feeds(self):
        connector = FeedConnector(active=True)
        page = await connector.run({"target": "https://www.youtube.com/@bgscet/videos", "origin": "lead"}, self.context(fetcher=Site({}).fetcher()))
        self.assertEqual(page.outcome, "snippet_only", "only the channel feed endpoint is read on YouTube")
        html = await connector.run({"target": "https://bgscet.ac.in/feed/", "origin": "lead"}, self.context(fetcher=Site({"https://bgscet.ac.in/feed/": (200, HOME)}).fetcher()))
        self.assertIn(html.outcome, {"not_a_feed", "content_type"})


class YouTubeTests(Fixture):
    async def test_channels_list_confirms_and_counts(self):
        channel_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(f"https://www.youtube.com/channel/{YT_CHANNEL}"), entity_id=self.entity_id, relation="official")
        handle_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.youtube.com/@bgscet"), entity_id=self.entity_id, relation="official")
        body = {"items": [{"id": YT_CHANNEL, "snippet": {"title": "BGSCET", "description": "Official channel. Visit https://www.bgscet.ac.in/ for admissions."}, "statistics": {"subscriberCount": "1520", "hiddenSubscriberCount": False}}]}
        client = api({("www.googleapis.com", "/youtube/v3/channels"): (200, body)})
        connector = YouTubeConnector(api_key="k", active=True, client=client)
        self.assertFalse(YouTubeConnector(active=True).enabled(), "no key, no connector")
        self.assertEqual({lead.target for lead in connector.plan(self.context())}, {channel_id, handle_id})
        await connector.run({"target": channel_id}, self.context())
        self.assertEqual(client.seen[-1].url.params["id"], YT_CHANNEL)
        asset = self.store.get_asset(INSTITUTION, channel_id)
        self.assertEqual((asset["followers"], asset["followers_source"], asset["platform_id"]), (1520, "youtube_api", YT_CHANNEL))
        kinds = {item["kind"] for item in self.store.list_evidence(INSTITUTION, asset_id=channel_id)}
        self.assertEqual(kinds, {"liveness", "api_identity", "backlink"})
        twin = await connector.run({"target": handle_id}, self.context())
        self.assertEqual(client.seen[-1].url.params["forHandle"], "@bgscet")
        self.assertIn(f"same channel as youtube:channel:{YT_CHANNEL}", twin.notes)
        missing = await YouTubeConnector(api_key="k", active=True, client=api({("www.googleapis.com", "/youtube/v3/channels"): (200, {"items": []})})).run({"target": channel_id}, self.context())
        self.assertEqual(missing.outcome, "not_found")
        refused = await YouTubeConnector(api_key="k", active=True, client=api({("www.googleapis.com", "/youtube/v3/channels"): (403, "")})).run({"target": channel_id}, self.context())
        self.assertEqual((refused.outcome, refused.failed), ("blocked", True))


class WikidataTests(Fixture):
    async def test_the_item_is_chosen_by_name_and_website_and_its_accounts_are_community_records(self):
        search = {"search": [{"id": "Q1"}, {"id": "Q2"}]}

        def claim(value):
            return [{"rank": "normal", "mainsnak": {"datavalue": {"value": value}}}]

        entities = {"entities": {
            "Q1": {"labels": {"en": {"value": "BGS College of Engineering and Technology"}}, "claims": {"P856": claim("https://other-bgs.example/")}},
            "Q2": {"labels": {"en": {"value": "BGS College of Engineering and Technology"}}, "aliases": {"en": [{"value": "BGSCET"}]}, "claims": {
                "P856": claim("https://bgscet.ac.in/"), "P2003": claim("bgscet_engg_coll"), "P2002": [{"rank": "deprecated", "mainsnak": {"datavalue": {"value": "old_handle"}}}],
            }},
        }}

        def answer(request):
            action = request.url.params["action"]
            return (200, search) if action == "wbsearchentities" else (200, entities)

        connector = WikidataConnector(active=True, client=api({("www.wikidata.org", "/w/api.php"): answer}))
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.entity_id], "look-alikes are not looked up")
        result = await connector.run({"target": self.entity_id}, self.context())
        self.assertEqual(result.outcome, "ok")
        self.assertIn("instagram:bgscet_engg_coll", result.new_assets)
        self.assertIsNone(self.store.find_asset(INSTITUTION, "x:old_handle"), "deprecated statements are ignored")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "C", "community data alone is one channel")
        self.assertEqual(self.grade_of("web:bgscet.ac.in"), "A", "the configured domain stays A")
        ambiguous = {"entities": {key: {**value, "claims": {}} for key, value in entities["entities"].items()}}
        unsure = WikidataConnector(active=True, client=api({("www.wikidata.org", "/w/api.php"): lambda request: (200, search) if request.url.params["action"] == "wbsearchentities" else (200, ambiguous)}))
        self.assertEqual((await unsure.run({"target": self.entity_id}, self.context())).outcome, "no_confident_item", "two same-named items and no website to tell them apart")


class CourtRecordsTests(Fixture):
    async def test_court_records_only_go_to_review(self):
        docs = {"docs": [
            {"tid": 101, "title": "State v. <b>BGS College of Engineering and Technology</b>", "headline": "fee dispute", "docsource": "Karnataka High Court", "publishdate": "2025-02-01"},
            {"tid": 102, "title": "Someone v. Another", "headline": "unrelated", "docsource": "Supreme Court", "publishdate": "2024-01-01"},
        ]}
        client = api({("api.indiankanoon.org", "/search/"): (200, docs)})
        connector = CourtRecordsConnector(api_token="t", active=True, client=client)
        self.assertFalse(CourtRecordsConnector(active=True).enabled())
        before = len(self.store.list_assets(INSTITUTION))
        result = await connector.run({"target": self.entity_id}, self.context())
        self.assertEqual([item["url"] for item in result.review], ["https://indiankanoon.org/doc/101/"])
        self.assertEqual(result.review[0]["title"], "State v. BGS College of Engineering and Technology")
        self.assertEqual(len(self.store.list_assets(INSTITUTION)), before, "nothing enters the map")
        self.assertEqual(client.seen[-1].headers["authorization"], "Token t")


class InfrastructureTests(Fixture):
    async def test_certificate_logs_find_subdomains_that_become_b(self):
        rows = [{"name_value": "alumni.bgscet.ac.in\n*.bgscet.ac.in"}, {"name_value": "www.bgscet.ac.in"}, {"name_value": "exam.bgscet.ac.in"}, {"name_value": "bgscet.ac.in.evil.example"}]
        connector = CertificateConnector(active=True, client=api({("crt.sh", "/"): (200, rows)}))
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.domain_id])
        result = await connector.run({"target": self.domain_id, "hops": 0}, self.context())
        self.assertEqual(sorted(lead.target for lead in result.leads), ["https://alumni.bgscet.ac.in/", "https://exam.bgscet.ac.in/"])
        site = Site({"https://alumni.bgscet.ac.in/": (200, "<html><head><title>Alumni portal</title></head><body>Sign in to the alumni network.</body></html>")})
        found = await LeadPageConnector().run({"target": "https://alumni.bgscet.ac.in/", "hops": 1}, self.context(fetcher=site.fetcher()))
        self.assertEqual(found.new_assets, ["web:alumni.bgscet.ac.in"])
        subdomain = self.store.find_asset(INSTITUTION, "web:alumni.bgscet.ac.in")
        self.assertEqual((subdomain["grade"], subdomain["relation"]), ("B", "official"), "a live subdomain of an A domain is B, even without naming the college")

    async def test_rdap_warns_before_a_domain_lapses(self):
        soon = (NOW + timedelta(days=20)).isoformat().replace("+00:00", "Z")
        client = api({("rdap.org", "/domain/bgscet.ac.in"): (200, {"events": [{"eventAction": "expiration", "eventDate": soon}], "status": ["active", "client transfer prohibited"]})})
        result = await RdapConnector(active=True, client=client).run({"target": self.domain_id}, self.context())
        self.assertEqual([incident["kind"] for incident in result.incidents], ["domain_expiring"])
        self.assertEqual(self.store.get_asset(INSTITUTION, self.domain_id)["registration_expires_at"][:10], soon[:10])
        lapsing = api({("rdap.org", "/domain/bgscet.ac.in"): (200, {"events": [], "status": ["redemption period"]})})
        self.assertEqual([item["kind"] for item in (await RdapConnector(active=True, client=lapsing).run({"target": self.domain_id}, self.context())).incidents], ["domain_lapsing"])
        free = await RdapConnector(active=True, client=api({})).run({"target": self.domain_id}, self.context())
        self.assertEqual(free.incidents[0]["kind"], "domain_unregistered")

    async def test_dns_spots_parking_and_vanished_names(self):
        def answers(request):
            kind = request.url.params["type"]
            if kind == "A":
                return 200, {"Status": 0, "Answer": [{"data": "93.184.216.34"}]}
            return 200, {"Status": 0, "Answer": [{"data": "ns1.sedoparking.com."}, {"data": "ns2.sedoparking.com."}]}

        result = await DnsConnector(active=True, client=api({("dns.google", "/resolve"): answers})).run({"target": self.domain_id}, self.context())
        self.assertEqual(result.incidents[0]["kind"], "site_parked")
        regrade(self.store, INSTITUTION, [self.domain_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, self.domain_id)["grade"], "D")
        gone = await DnsConnector(active=True, client=api({("dns.google", "/resolve"): (200, {"Status": 3})})).run({"target": self.domain_id}, self.context())
        self.assertEqual((gone.outcome, gone.incidents[0]["kind"]), ("nxdomain", "domain_not_resolving"))


HIJACKED_CAPTURE = "<html><head><title>Slot Gacor Maxwin Togel</title></head><body>situs slot gacor togel judi maxwin bandar</body></html>"
OLD_HOME = """<html><head><title>Sri Adichunchanagiri Mahasamsthana Math</title></head><body><p>Welcome</p>
<footer><a href="https://www.facebook.com/adichunchanagirimath">Facebook</a> <a href="https://www.youtube.com/@adichunchanagiri">YouTube</a></footer></body></html>"""


class WaybackTests(Fixture):
    async def test_a_lapsed_official_site_is_read_from_its_last_healthy_capture(self):
        from app.internet_intelligence.profile import InstitutionProfile

        math = InstitutionProfile(INSTITUTION, "Sri Adichunchanagiri Mahasamsthana Math", "Nagamangala", official_domains=["acmbgs.org"])
        domain_id = [asset_id for asset_id in sync_profile(self.store, math)["domains_added"]][0]
        self.store.add_evidence(INSTITUTION, asset_id=domain_id, kind="integrity", polarity="refutes", detail="parked:parked_lander", source_url="https://acmbgs.org/", channel="site:acmbgs.org")
        regrade(self.store, INSTITUTION, [domain_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, domain_id)["grade"], "D")
        cdx = [["timestamp", "original", "statuscode", "mimetype"], ["20190101000000", "https://acmbgs.org/", "200", "text/html"], ["20230101000000", "https://acmbgs.org/", "200", "text/html"]]
        routes = {
            ("web.archive.org", "/cdx/search/cdx"): (200, cdx),
            ("web.archive.org", "/web/20230101000000id_/https://acmbgs.org/"): (200, HIJACKED_CAPTURE),
            ("web.archive.org", "/web/20190101000000id_/https://acmbgs.org/"): (200, OLD_HOME),
        }
        connector = WaybackConnector(active=True, client=api(routes))
        self.assertIn(domain_id, [lead.target for lead in connector.plan(self.context())], "failing official domains are looked up in the archive")
        self.assertNotIn(self.domain_id, [lead.target for lead in connector.plan(self.context())], "healthy ones are not")
        result = await connector.run({"target": domain_id}, self.context())
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(sorted(result.new_assets), ["facebook:adichunchanagirimath", "youtube:@adichunchanagiri"])
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("facebook:adichunchanagirimath"), "A-arch", "official then, per the archive; never A")
        empty = await WaybackConnector(active=True, client=api({("web.archive.org", "/cdx/search/cdx"): (200, [])})).run({"target": domain_id}, self.context())
        self.assertEqual(empty.outcome, "no_captures")


LINKTREE = """<html><head><title>BGSCET | Linktree</title></head><body><main>
<a href="https://www.instagram.com/bgscet_mba/">MBA</a> <a href="https://x.com/BGSCET_ENGG_COL">X</a> <a href="https://bgscet-alumni.example/">Alumni</a> <a href="https://linktr.ee/s/about">About Linktree</a>
</main></body></html>"""
NIRF = """<html><head><title>NIRF 2026 Engineering</title></head><body><header><a href="https://www.facebook.com/nirfindia">NIRF on Facebook</a></header>
<main><table><tr><td>BGS College of Engineering and Technology</td><td><a href="https://bgscet.ac.in/">bgscet.ac.in</a></td></tr>
<tr><td>Other College</td><td><a href="https://other-college.example/">other-college.example</a></td></tr></table></main></body></html>"""


class HubAndDirectoryTests(Fixture):
    async def test_a_hub_on_the_official_site_passes_on_one_grade_less(self):
        home = HOME.replace('<footer>', '<footer><a href="https://linktr.ee/bgscet">Links</a> ')
        await OfficialSiteHarvester(Site({"https://bgscet.ac.in/": (200, home)}).fetcher(), self.store).harvest(INSTITUTION, self.domain_id)
        hub = self.store.find_asset(INSTITUTION, "linktree:bgscet")
        self.assertEqual(hub["grade"], "A")
        connector = LinkHubConnector(active=True)
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [hub["asset_id"]])
        result = await connector.run({"target": hub["asset_id"], "hops": 0}, self.context(fetcher=Site({"https://linktr.ee/bgscet": (200, LINKTREE)}).fetcher()))
        self.assertEqual(sorted(result.new_assets), ["instagram:bgscet_mba", "x:bgscet_engg_col"])
        self.assertEqual([lead.target for lead in result.leads], ["https://bgscet-alumni.example/"])
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("instagram:bgscet_mba"), "B")

    async def test_regulator_listings_anchor_and_other_listings_corroborate(self):
        connector = DirectoryConnector(active=True)
        site = Site({"https://www.nirfindia.org/2026/engineering": (200, NIRF)})
        result = await connector.run({"target": "https://www.nirfindia.org/2026/engineering"}, self.context(fetcher=site.fetcher()))
        self.assertEqual(result.new_assets, [], "the listed domain is already mapped")
        evidence = [item for item in self.store.list_evidence(INSTITUTION, asset_id=self.domain_id) if item["kind"] == "directory_record"]
        self.assertEqual(evidence[0]["detail"], "authority:nirf")
        self.assertIsNone(self.store.find_asset(INSTITUTION, "facebook:nirfindia"), "the directory's own accounts are not the college's")
        self.assertIsNone(self.store.find_asset(INSTITUTION, "web:other-college.example"))
        listing = NIRF.replace("NIRF 2026 Engineering", "Unstop college page")
        other = await connector.run({"target": "https://unstop.com/college/bgscet"}, self.context(fetcher=Site({"https://unstop.com/college/bgscet": (200, listing)}).fetcher()))
        self.assertEqual(other.outcome, "ok")
        community = [item for item in self.store.list_evidence(INSTITUTION, asset_id=self.domain_id) if item["kind"] == "community_record"]
        self.assertEqual(community[0]["detail"], "listing:unstop.com")


class WiringTests(unittest.TestCase):
    def test_connectors_are_off_until_named_and_keyed(self):
        from app.api.platform_runtime import map_connectors

        self.assertEqual(map_connectors(AppSettings()).names(), ["lead_page", "official_site", "owner_claims", "recheck"], "only readers of public pages and the institution's own domains are on")
        on = AppSettings(intelligence_connectors=("wikidata", "rdap", "youtube"), intelligence_youtube_api_key="k")
        self.assertEqual(map_connectors(on).names(), ["lead_page", "official_site", "owner_claims", "rdap", "recheck", "wikidata", "youtube"])
        self.assertIsNotNone(map_connectors(AppSettings()).get("owner_claims").dns, "the DNS TXT method is offered, so it works without the dns connector")
        for bad, message in (
            (AppSettings(intelligence_connectors=("scraper",)), "unknown connectors"),
            (AppSettings(intelligence_connectors=("youtube",)), "YOUTUBE_API_KEY"),
            (AppSettings(intelligence_connectors=("court_records",)), "INDIANKANOON_TOKEN"),
            (AppSettings(intelligence_connectors=("search",)), "SEARCH_PROVIDER"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                bad.ensure_safe_for_production()

    def test_the_engine_carries_review_items_and_incidents_out_of_a_tick(self):
        import asyncio

        store = MapStore(":memory:", suppression_key=b"test-key")
        entity_id = sync_profile(store, PROFILE)["entity_id"]
        client = api({("api.indiankanoon.org", "/search/"): (200, {"docs": [{"tid": 7, "title": "BGS College of Engineering and Technology v. State", "headline": ""}]})})
        engine = MapEngine(store, ConnectorRegistry([CourtRecordsConnector(api_token="t", active=True, client=client)]), clock=lambda: NOW)
        result = asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual(result["counts"]["review"], 1)
        self.assertEqual((result["review"][0]["connector"], result["review"][0]["entity_id"]), ("court_records", entity_id))


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class SourceRouteTests(unittest.TestCase):
    def test_managers_add_directory_pages(self):
        college = f"dir_college_{uuid4().hex[:8]}"  # a fresh tenant, so the test passes on a reused database too
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "dir-principal", "X-Demo-Role": "principal", "X-Demo-College": college}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        with mock.patch.object(platform, "intelligence_map", MapService(store)):
            added = client.post("/v1/intelligence/map/sources", headers=headers, json={"connector": "directory", "target": "https://www.nirfindia.org/2026/engineering"})
            self.assertEqual(added.status_code, 200, added.text)
            self.assertEqual((added.json()["created"], added.json()["enabled"]), (True, False))
            self.assertEqual(client.post("/v1/intelligence/map/sources", headers=headers, json={"connector": "search", "target": "x"}).status_code, 422)
            self.assertEqual(client.post("/v1/intelligence/map/sources", headers=headers, json={"connector": "directory", "target": "file:///etc/passwd"}).status_code, 422)
            self.assertEqual(client.post("/v1/intelligence/map/sources", headers={**headers, "X-Demo-Role": "faculty"}, json={"connector": "directory", "target": "https://a.example/"}).status_code, 403)
            self.assertIn("intelligence.map.add_source", {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)})


if __name__ == "__main__":
    unittest.main()
