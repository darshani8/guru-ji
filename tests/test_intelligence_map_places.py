import unittest
from datetime import datetime, timedelta, timezone

from app.config.settings import AppSettings
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.places import GooglePlayConnector, OpenStreetMapConnector
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.pipeline import regrade
from test_intelligence_map_connectors import INSTITUTION, Fixture, api
from test_intelligence_map_engine import HOME, Site

NOMINATIM = ("nominatim.openstreetmap.org", "/search")
PLACE = {
    "osm_type": "way", "osm_id": 123456, "name": "BGS College of Engineering and Technology",
    "namedetails": {"name": "BGS College of Engineering and Technology", "name:kn": "ಬಿಜಿಎಸ್ ಇಂಜಿನಿಯರಿಂಗ್ ಮತ್ತು ತಂತ್ರಜ್ಞಾನ ಕಾಲೇಜು"},
    "extratags": {
        "website": "https://bgscet.ac.in/", "contact:instagram": "bgscet_engg_coll", "contact:facebook": "https://www.facebook.com/bgscet.official",
        "contact:linkedin": "https://www.linkedin.com/in/some-person-123/",
    },
}
LOOKALIKE_PLACE = {"osm_type": "node", "osm_id": 99, "name": "BGS Group of Institutions", "extratags": {"contact:instagram": "bgs_group"}}
LISTING = """<html><head><title>BGSCET Connect - Apps on Google Play</title></head><body>
<a href="https://play.google.com/store/apps/dev?id=555">BGS College of Engineering and Technology</a>
<a href="https://support.google.com/googleplay">Help</a>
<a href="https://www.bgscet.ac.in/app">Website</a>
<a href="mailto:app@bgscet.ac.in">Support email</a>
</body></html>"""


class OpenStreetMapTests(Fixture):
    async def test_the_place_named_like_the_entity_lists_its_accounts_as_community_records(self):
        client = api({NOMINATIM: (200, [LOOKALIKE_PLACE, PLACE])})
        connector = OpenStreetMapConnector(active=True, client=client, min_gap_seconds=0)
        self.assertFalse(OpenStreetMapConnector().enabled(), "off until configured")
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [self.entity_id], "look-alikes are not looked up")
        result = await connector.run({"target": self.entity_id}, self.context())
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(sorted(result.new_assets), ["facebook:bgscet.official", "instagram:bgscet_engg_coll"])
        self.assertIsNone(self.store.find_asset(INSTITUTION, "instagram:bgs_group"), "the look-alike's place is never chosen")
        self.assertIsNone(self.store.find_asset(INSTITUTION, "linkedin:in:some-person-123"), "a person's profile is never taken from a tag")
        request = client.seen[-1]
        self.assertEqual((request.url.params["format"], request.url.params["extratags"]), ("jsonv2", "1"))
        self.assertTrue(request.headers["user-agent"].startswith("AgenticSaffron-"), "Nominatim requires an identifying User-Agent")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "C", "community data alone is one channel")
        self.assertEqual(self.grade_of("web:bgscet.ac.in"), "A", "the configured domain stays A")
        evidence = [item for item in self.store.list_evidence(INSTITUTION, asset_id=self.store.find_asset(INSTITUTION, "instagram:bgscet_engg_coll")["asset_id"])]
        self.assertEqual((evidence[0]["kind"], evidence[0]["detail"], evidence[0]["source_url"]), ("community_record", "osm:way/123456:contact:instagram", "https://www.openstreetmap.org/way/123456"))

    async def test_two_same_named_places_are_told_apart_by_website_or_not_at_all(self):
        other = {**PLACE, "osm_id": 777, "extratags": {"website": "https://other-bgs.example/", "contact:instagram": "someone_else"}}
        chosen = await OpenStreetMapConnector(active=True, client=api({NOMINATIM: (200, [other, PLACE])}), min_gap_seconds=0).run({"target": self.entity_id}, self.context())
        self.assertIn("instagram:bgscet_engg_coll", chosen.new_assets)
        self.assertIsNone(self.store.find_asset(INSTITUTION, "instagram:someone_else"))
        bare = {key: value for key, value in PLACE.items() if key != "extratags"}
        unsure = await OpenStreetMapConnector(active=True, client=api({NOMINATIM: (200, [bare, {**bare, "osm_id": 5}])}), min_gap_seconds=0).run({"target": self.entity_id}, self.context())
        self.assertEqual(unsure.outcome, "no_confident_place")
        refused = await OpenStreetMapConnector(active=True, client=api({NOMINATIM: (429, "")}), min_gap_seconds=0).run({"target": self.entity_id}, self.context())
        self.assertEqual((refused.outcome, refused.failed), ("blocked", True))

    async def test_requests_are_spaced_by_the_usage_policy(self):
        connector = OpenStreetMapConnector(active=True, client=api({NOMINATIM: (200, [])}), min_gap_seconds=0.2)
        loop = __import__("asyncio").get_running_loop()
        started = loop.time()
        await connector.run({"target": self.entity_id}, self.context())
        await connector.run({"target": self.entity_id}, self.context())
        self.assertGreaterEqual(loop.time() - started, 0.19)


class GooglePlayTests(Fixture):
    def test_app_listings_and_developer_pages_are_assets(self):
        app = asset_ref("https://play.google.com/store/apps/details?id=in.ac.bgscet.connect&hl=en_IN&gl=US")
        self.assertEqual((app.platform, app.kind, app.key, app.url), ("google_play", "account", "google_play:app:in.ac.bgscet.connect", "https://play.google.com/store/apps/details?id=in.ac.bgscet.connect"))
        self.assertEqual(asset_ref("play.google.com/store/apps/dev?id=555").key, "google_play:dev:555")
        self.assertEqual(asset_ref("https://play.google.com/store/apps/details?id=../../etc").kind, "page", "not an application ID")
        self.assertFalse(app.is_social)

    async def test_a_listing_is_rechecked_and_its_developer_links_back(self):
        home = HOME.replace("<footer>", '<footer><a href="https://play.google.com/store/apps/details?id=in.ac.bgscet.connect">Get it on Google Play</a> ')
        await OfficialSiteHarvester(Site({"https://bgscet.ac.in/": (200, home)}).fetcher(), self.store).harvest(INSTITUTION, self.domain_id)
        app = self.store.find_asset(INSTITUTION, "google_play:app:in.ac.bgscet.connect")
        self.assertEqual(app["grade"], "A", "a footer badge on the official site vouches for the app")
        connector = GooglePlayConnector(active=True)
        self.assertEqual([lead.target for lead in connector.plan(self.context())], [app["asset_id"]])
        site = Site({"https://play.google.com/store/apps/details": (200, LISTING)})
        result = await connector.run({"target": app["asset_id"]}, self.context(fetcher=site.fetcher()))
        self.assertEqual(result.outcome, "ok")
        backlinks = [item for item in self.store.list_evidence(INSTITUTION, asset_id=app["asset_id"]) if item["kind"] == "backlink"]
        self.assertEqual([item["detail"] for item in backlinks], ["listing's developer website is on bgscet.ac.in"])
        self.assertEqual(self.store.find_asset(INSTITUTION, "google_play:dev:555"), None, "links on the listing are not taken as accounts")

    async def test_gone_twice_is_dead_and_refused_is_blocked(self):
        ref = asset_ref("https://play.google.com/store/apps/details?id=in.ac.bgscet.old")
        asset_id, _ = self.store.upsert_asset(INSTITUTION, ref, entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="search_snippet", channel="search:t", detail="", observed_via="index")
        connector = GooglePlayConnector(active=True)
        gone = Site({})
        await connector.run({"target": asset_id}, self.context(fetcher=gone.fetcher()))
        regrade(self.store, INSTITUTION, [asset_id])
        self.assertNotEqual(self.store.get_asset(INSTITUTION, asset_id)["status"], "dead", "one miss is not dead")
        # A miss two days earlier in the same unbroken run of failures confirms it.
        earlier = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="liveness", polarity="refutes", detail="not_found:404", channel="google_play", observed_via="live", observed_at=earlier)
        await connector.run({"target": asset_id}, self.context(fetcher=gone.fetcher()))
        regrade(self.store, INSTITUTION, [asset_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, asset_id)["status"], "dead")
        self.assertEqual(self.store.get_asset(INSTITUTION, asset_id)["grade"], "D")
        walled_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://play.google.com/store/apps/details?id=in.ac.bgscet.walled"), entity_id=self.entity_id, relation="official")
        refused = await connector.run({"target": walled_id}, self.context(fetcher=Site({"https://play.google.com/store/apps/details": (403, "")}).fetcher()))
        self.assertTrue(refused.failed)
        regrade(self.store, INSTITUTION, [walled_id])
        self.assertEqual(self.store.get_asset(INSTITUTION, walled_id)["status"], "blocked", "refused is not dead")

    def test_several_apps_are_not_a_dispute(self):
        for package in ("in.ac.bgscet.one", "in.ac.bgscet.two"):
            asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(f"https://play.google.com/store/apps/details?id={package}"), entity_id=self.entity_id, relation="official")
            for channel in ("search:a", "wikidata"):
                self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="community_record" if channel == "wikidata" else "search_snippet", channel=channel, detail="", observed_via="index")
        regrade(self.store, INSTITUTION)
        self.assertEqual({asset["grade"] for asset in self.store.iter_assets(INSTITUTION, platform="google_play")}, {"B"})


class WiringTests(unittest.TestCase):
    def test_both_are_off_until_named(self):
        from app.api.platform_runtime import map_connectors

        self.assertNotIn("openstreetmap", map_connectors(AppSettings()).names())
        on = map_connectors(AppSettings(intelligence_connectors=("openstreetmap", "google_play")))
        self.assertIn("openstreetmap", on.names())
        self.assertIn("google_play", on.names())
        described = {item["name"]: item for item in on.describe()}
        self.assertEqual((described["openstreetmap"]["access_mode"], described["google_play"]["access_mode"]), ("official_api", "public_page"))


if __name__ == "__main__":
    unittest.main()
