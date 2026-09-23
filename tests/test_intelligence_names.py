"""Names, places and personal accounts: what an entity is searched and matched by, and what a seed must never import."""

import json
import math
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from app.internet_intelligence.entity_resolution import HIGH, LOW, NOT_MATCHED, resolve_entity
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import WikidataConnector
from app.internet_intelligence.map.connectors.base import ConnectorContext
from app.internet_intelligence.map.connectors.common import ApiClient, _compact, _words, entity_profile, match_entity, names_entity
from app.internet_intelligence.map.connectors.search import SearchConnector
from app.internet_intelligence.map.connectors.web import LeadPageConnector
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.seed import import_seed, load_default_seed, parse_lookalikes, subject_names
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.query_generator import DEFAULT_TOPICS, generate_queries
from app.internet_intelligence.search import StaticSearchProvider, hits_from_fixture
from app.internet_intelligence.service import InternetIntelligenceService
from app.internet_intelligence.store import IntelligenceStore
from test_intelligence_map_verification import fetcher_for

I = "bgscet"
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
KANNADA_NAME = "ಬಿಜಿಎಸ್ ಎಂಜಿನಿಯರಿಂಗ್ ಮತ್ತು ತಂತ್ರಜ್ಞಾನ ಕಾಲೇಜು"
PROFILE = InstitutionProfile(I, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])
PERSONAL = {
    "linkedin:in:cse-bgscet-775259355", "linkedin:in:bgs-college-of-engineering-and-technology-14043125a", "linkedin:in:sjbit-bengaluru-34a401172",
    "linkedin:in:bgsit-adichunchanagiri-university-acu-bg-nagar-780401217", "linkedin:in:sri-sri-sri-dr-nirmalanandanatha-mahaswamiji-84bb11322",
    "whatsapp:918884888883", "whatsapp:919606485137", "whatsapp:919035020091", "whatsapp:917337702225",
}


class QueryRotationTests(unittest.TestCase):
    def test_every_alias_and_keyword_is_searched_within_one_rotation_cycle(self):
        profile = InstitutionProfile(I, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET", "BGS CET", "BGS College of Engineering & Technology", KANNADA_NAME], keywords=["mahalakshmipuram", "vtu", "advaya hackathon"])
        identity, reserved = 1 + len(profile.aliases) + len(profile.keywords), max(1, 8 // 3)
        seen = {query for rotation in range(math.ceil(identity / reserved)) for query in generate_queries(profile, topics=list(DEFAULT_TOPICS), max_queries=8, rotation=rotation, now=NOW)}
        for alias in profile.aliases:
            self.assertTrue(any(query.startswith(f'"{alias}"') for query in seen), alias)
        for keyword in profile.keywords:
            self.assertIn(f'"{profile.name}" {keyword}', seen)


class ShortAliasTests(unittest.IsolatedAsyncioTestCase):
    BGS = InstitutionProfile(I, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET", "BGS"], official_domains=["bgscet.ac.in"], monitoring_enabled=True)

    def test_a_page_naming_only_a_short_alias_is_for_review(self):
        survey = resolve_entity(self.BGS, url="https://www.bgs.ac.uk/news/map", title="BGS releases new geological map", text="The BGS published a new map of the Scottish Highlands.")
        self.assertEqual(survey.level, LOW)
        self.assertIn("generic_name_without_location", survey.reasons)
        self.assertEqual(resolve_entity(self.BGS, url="https://news.example/x", title="BGSCET fest, Bengaluru", text="The BGSCET fest in Bengaluru drew crowds.").level, HIGH, "the place makes the short name ours")

    def test_an_acronym_is_a_name_only_as_written(self):
        aims = InstitutionProfile(I, "Adichunchanagiri Institute of Medical Sciences", "Mandya", aliases=["AIMS"])
        verb = resolve_entity(aims, url="https://news.example/x", title="Mandya police aims to curb sand mining; case registered", text="Police in Mandya aims to stop illegal sand mining.")
        self.assertEqual((verb.level, verb.reasons), (NOT_MATCHED, ("name_not_found",)), "the verb 'aims' beside the place is not AIMS")
        self.assertEqual(resolve_entity(aims, url="https://news.example/y", title="AIMS, B.G. Nagara holds a health camp", text="Doctors from AIMS examined 400 villagers in Mandya.").level, HIGH)
        ait = InstitutionProfile(I, "Adichunchanagiri Institute of Technology", "Chikkamagaluru", aliases=["AIT"])
        self.assertEqual(resolve_entity(ait, url="https://news.example/z", title="Chikkamagaluru: govt ait scheme", text="").level, NOT_MATCHED)
        # Only prose is case-sensitive: a handle or host in the URL counts in any case, and so does a name that is not an acronym.
        bgscet = InstitutionProfile(I, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"], social_accounts=["bgscet"])
        self.assertIn("known_social_account", resolve_entity(bgscet, url="https://www.instagram.com/BGSCET/", title="", text="").reasons)
        self.assertIn("official_domain", resolve_entity(bgscet, url="https://www.BGSCET.ac.in/news", title="aims and objectives", text="").reasons)
        self.assertIn("name_in_title:BGS College of Engineering and Technology", resolve_entity(bgscet, url="https://news.example/w", title="bgs college of engineering and technology, bengaluru", text="").reasons)

    async def test_the_investigation_keeps_it_out_of_the_findings(self):
        store = IntelligenceStore(":memory:")
        store.save_profile(self.BGS, updated_by="t")
        hits = hits_from_fixture([{"url": "https://www.bgs.ac.uk/news/map", "title": "BGS releases new geological map", "snippet": "The BGS published a new map of the Scottish Highlands.", "published_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}])
        report, kept = await InternetIntelligenceService(store, StaticSearchProvider(hits)).collect(I, requested_by="t", question=None, window_days=7, topics=None, max_results=10, persist=False)
        self.assertEqual(kept, [])
        self.assertEqual([item["url"] for item in report["review_candidates"]], ["https://www.bgs.ac.uk/news/map"])


class LookalikeScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_page_a_mapped_lookalike_matches_as_well_is_excluded(self):
        store = IntelligenceStore(":memory:")
        store.save_profile(InstitutionProfile("i", "Government First Grade College", "Tumakuru"), updated_by="t")
        map_store = MapStore(":memory:", suppression_key=b"k")
        map_store.upsert_entity("i", name="Government First Grade College Mysuru", kind="lookalike", locations=["Mysuru"])
        when = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        hits = hits_from_fixture([
            {"url": "https://news.example/joint", "title": "Government First Grade College Mysuru hosts Tumakuru students for a fest", "snippet": "Government First Grade College Mysuru held a fest event for students from Tumakuru.", "published_at": when},
            {"url": "https://news.example/ours", "title": "Government First Grade College Tumakuru fest", "snippet": "The fest event at Government First Grade College, Tumakuru drew crowds.", "published_at": when},
        ])
        plain, _ = await InternetIntelligenceService(store, StaticSearchProvider(hits)).collect("i", requested_by="t", question=None, window_days=7, topics=None, max_results=10, persist=False)
        self.assertEqual(len(plain["findings"]), 2, "without the map both pages look like ours")
        screened, kept = await InternetIntelligenceService(store, StaticSearchProvider(hits), map_store=map_store).collect("i", requested_by="t", question=None, window_days=7, topics=None, max_results=10, persist=False)
        self.assertEqual([item["url"] for item in kept], ["https://news.example/ours"])
        self.assertEqual(screened["excluded"], {"lookalike_match": 1})


class KannadaTests(unittest.IsolatedAsyncioTestCase):
    ENTITY = {"name": "BGS College of Engineering and Technology", "names": [KANNADA_NAME], "locations": ["Bengaluru", "ಬೆಂಗಳೂರು"]}

    def test_kannada_words_stay_whole(self):
        self.assertEqual(_words(KANNADA_NAME), KANNADA_NAME.split(), r"not \w, which splits at vowel signs")
        self.assertEqual(_compact("ಆದಿಚುಂಚನಗಿರಿ ವಿಶ್ವವಿದ್ಯಾಲಯ"), "ಆದಿಚುಂಚನಗಿರಿವಿಶ್ವವಿದ್ಯಾಲಯ")

    def test_a_kannada_display_name_names_the_entity(self):
        self.assertTrue(names_entity(self.ENTITY, handle="x", title=f"{KANNADA_NAME} (@x) • Instagram"))
        self.assertFalse(names_entity(self.ENTITY, handle="x", title=f"Rahul Kumar | {KANNADA_NAME}"), "a person's page that mentions the college")

    def test_a_kannada_article_naming_the_kannada_place_is_high(self):
        article = f"{KANNADA_NAME} ಬೆಂಗಳೂರು ಹ್ಯಾಕಥಾನ್"
        profile = entity_profile(self.ENTITY, I, f"{article} {article}")
        self.assertEqual(profile.location, "ಬೆಂಗಳೂರು", "the location the page names is the one weighed")
        self.assertEqual(resolve_entity(profile, url="https://kannada.news.example/x", title=article, text=article).level, HIGH)
        self.assertEqual(entity_profile(self.ENTITY, I).location, "Bengaluru")

    async def test_wikidata_is_asked_for_kannada_labels_and_matches_them(self):
        store = MapStore(":memory:", suppression_key=b"k")
        entity_id = store.upsert_entity(I, name="Adichunchanagiri University", names=["ಆದಿಚುಂಚನಗಿರಿ ವಿಶ್ವವಿದ್ಯಾಲಯ"])
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.params["action"] == "wbsearchentities":
                return httpx.Response(200, content=json.dumps({"search": [{"id": "Q7"}]}), request=request)
            item = {"labels": {"kn": {"value": "ಆದಿಚುಂಚನಗಿರಿ ವಿಶ್ವವಿದ್ಯಾಲಯ"}}, "claims": {"P856": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "https://acu.edu.in/"}}}]}}
            return httpx.Response(200, content=json.dumps({"entities": {"Q7": item}}), request=request)

        result = await WikidataConnector(active=True, client=ApiClient(transport=httpx.MockTransport(handler))).run({"target": entity_id}, ConnectorContext(store, I, "r", NOW))
        self.assertEqual(seen[-1].url.params["languages"], "en|kn")
        self.assertEqual(result.new_assets, ["web:acu.edu.in"], "an item labelled only in Kannada is still found by the Kannada name")


class SeedNamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.rows, self.lookalikes = load_default_seed()

    def seed(self, *groups, holdout=20):
        return import_seed(self.store, I, self.rows, lookalikes=self.lookalikes, groups=list(groups) or None, holdout_percent=holdout)

    def ours(self):
        return [entity for entity in self.store.list_entities(I) if entity["kind"] != "lookalike"]

    def college(self):
        return next(entity for entity in self.ours() if "BGSCET" in entity["names"])

    def context(self, **kwargs):
        return ConnectorContext(self.store, I, "run-1", NOW, **kwargs)

    def test_short_forms_in_brackets_are_names(self):
        self.assertEqual(subject_names("BGS College of Engineering & Technology (BGSCET)"), ("BGS College of Engineering & Technology", ["BGSCET", "BGS College of Engineering and Technology"]))
        for subject in ("Sri Adichunchanagiri College of Pharmacy (NIRF #80)", "BGS Public School, Kengeri (CBSE)", "BGS Institute of Nursing Sciences, ACU (Nagamangala)"):
            self.assertEqual(subject_names(subject), (subject, []), subject)

    def test_the_seeded_college_has_its_names_and_places_and_matches_them(self):
        self.seed("BGSCET")
        college = self.college()
        self.assertEqual(college["name"], "BGS College of Engineering and Technology", "the spelling the college's own site uses")
        self.assertIn("BGS College of Engineering & Technology", college["names"])
        self.assertEqual(college["locations"][0], "Bengaluru")
        self.assertIn("ಬೆಂಗಳೂರು", college["locations"])
        hit = match_entity(self.context(), url="https://news.example/fest", title="BGSCET fest, Bengaluru", text="BGSCET fest, Bengaluru")
        self.assertEqual(hit.entity["entity_id"], college["entity_id"])
        names = {entity["name"] for entity in self.ours()}
        for leftover in ("Duplicate principal channel", "Advaya 2025 announcement", "CSE BGSCET channel", "BGSCET Mahalakshmipuram"):
            self.assertNotIn(leftover, names, "leftover sweep text is a label, not an entity")
        self.assertEqual(self.store.find_asset(I, "youtube:@principalbgscet")["entity_id"], college["entity_id"])

    def test_one_college_whichever_comes_first_the_profile_or_the_seed(self):
        self.seed("BGSCET")
        synced = sync_profile(self.store, PROFILE)
        self.assertEqual(len([entity for entity in self.ours() if entity["kind"] == "institution"]), 1)
        self.assertEqual(synced["entity_id"], self.college()["entity_id"])
        self.assertEqual(self.store.find_asset(I, "web:bgscet.ac.in")["entity_id"], synced["entity_id"])
        fresh = MapStore(":memory:", suppression_key=b"test-key")
        profile_first = sync_profile(fresh, PROFILE)["entity_id"]
        import_seed(fresh, I, self.rows, groups=["BGSCET"])
        self.assertEqual([entity["entity_id"] for entity in fresh.list_entities(I, kind="institution")], [profile_first])
        self.assertIn("BGS College of Engineering & Technology", fresh.get_entity(I, profile_first)["names"])

    async def test_search_takes_each_name_in_turn_with_the_place(self):
        self.seed("BGSCET")
        college = self.college()
        search = StaticSearchProvider(())
        names = [college["name"], *college["names"]]
        for runs in range(len(names)):
            await SearchConnector(active=True).run({"target": f"{college['entity_id']}|instagram.com", "runs": runs}, self.context(search=search))
        self.assertEqual(search.calls, [f'"{name}" Bengaluru' for name in names])

    def test_the_swamiji_is_matched_by_his_short_and_kannada_names(self):
        self.seed("Mutt & Swamiji", holdout=0)
        swamiji = next(entity for entity in self.ours() if entity["kind"] == "role" and "Nirmalanandanatha Swamiji" in entity["names"])
        hit = match_entity(self.context(), url="https://news.example/visit", title="Nirmalanandanatha Swamiji visits Mandya", text="Sri Nirmalanandanatha Swamiji of Adichunchanagiri visited Mandya.")
        self.assertEqual(hit.entity["entity_id"], swamiji["entity_id"])
        kannada = "ಶ್ರೀ ಶ್ರೀ ಶ್ರೀ ಡಾ. ನಿರ್ಮಲಾನಂದನಾಥ ಮಹಾಸ್ವಾಮೀಜಿ ಮಂಡ್ಯ"
        self.assertEqual(match_entity(self.context(), url="https://kannada.news.example/x", title=kannada, text=kannada).entity["entity_id"], swamiji["entity_id"])
        self.assertEqual(self.store.get_entity(I, swamiji["parent_id"])["name"], "Sri Adichunchanagiri Mahasamsthana Math")

    def test_a_pilot_import_maps_no_other_groups_organisation(self):
        self.seed("BGSCET")
        self.assertEqual({entity["kind"] for entity in self.ours()} - {"institution", "unit", "branch"}, set(), "no organisation (the Math) or role")

    def test_held_out_subjects_never_become_entities(self):
        self.seed("BGSCET")
        self.assertIn("linkedin:showcase:bgscet-csd", self.store.holdout_keys(I))
        self.assertNotIn("BGSCET — CSD", {entity["name"] for entity in self.ours()})

    def test_personal_profiles_and_phone_numbers_are_not_imported(self):
        summary = self.seed()
        created = {asset["asset_key"] for asset in self.store.list_assets(I, limit=5000)} | self.store.holdout_keys(I)
        self.assertFalse(PERSONAL & created)
        self.assertEqual(summary.personal, len(PERSONAL))
        # An /in/ profile whose own handle is the college's name passes names_entity, like search's check.
        self.assertIn("linkedin:in:sri-adichunchanagiri-college-of-pharmacy", created)
        self.assertIn("whatsapp:group:B8VAkhaf35gH1Fxy53jApc".lower(), {key.lower() for key in created}, "a group invite is not a phone number")
        pilot = MapStore(":memory:", suppression_key=b"test-key")
        import_seed(pilot, I, self.rows, groups=["BGSCET"])
        self.assertIsNone(pilot.find_asset(I, "linkedin:in:cse-bgscet-775259355"), "the HOD's profile")

    def test_lookalikes_without_a_canary_are_reported(self):
        warnings = self.seed("BGSCET").warnings
        self.assertEqual(len(warnings), 1)
        for name in ("Vishwa Vokkaligara Mahasamsthana Math", "Swami Nirmalananda Giri"):
            self.assertIn(name, warnings[0])
        self.assertEqual(import_seed(self.store, I, [], lookalikes=parse_lookalikes("name\tnames\tlocations\turl\twhy\nParked\t\t\thttps://acmbgs.org/\tparked\n")).warnings, [])

    async def test_seeded_sites_are_queued_for_lead_page_which_reads_them(self):
        summary = self.seed("BGSCET")
        leads = {source["target"]: source for source in self.store.list_sources(I, connector="lead_page")}
        self.assertEqual(set(leads), {"https://bgscet.ac.in/", "https://mba.bgscet.ac.in/", "https://advaya2.vercel.app/"}, "domains only; held-out sushumna2k26.bgscet.in is not among them")
        self.assertEqual(summary.leads, 3)
        self.assertEqual({(source["origin"], source["hops"]) for source in leads.values()}, {("seed", 0)})
        domain_id = sync_profile(self.store, PROFILE)["domains_added"] or [self.store.find_asset(I, "web:bgscet.ac.in")["asset_id"]]
        regrade(self.store, I, domain_id)
        advaya = "<html><head><title>Advaya 2.0 hackathon</title></head><body><main><p>Advaya 2.0, the hackathon of BGS College of Engineering and Technology, Bengaluru.</p></main></body></html>"
        fetcher = fetcher_for({"https://mba.bgscet.ac.in/": (200, "<html><head><title>MBA</title></head><body>School of Management</body></html>"), "https://advaya2.vercel.app/": (200, advaya)})
        for target in ("https://mba.bgscet.ac.in/", "https://advaya2.vercel.app/"):
            result = await LeadPageConnector().run(leads[target], self.context(fetcher=fetcher))
            self.assertEqual(result.outcome, "ok", target)
        mba = self.store.find_asset(I, "web:mba.bgscet.ac.in")
        self.assertEqual((mba["grade"], mba["relation"]), ("B", "official"), "a live subdomain of the configured domain")
        self.assertIn("backlink", {row["kind"] for row in self.store.list_evidence(I, asset_id=self.store.find_asset(I, "web:advaya2.vercel.app")["asset_id"])})
        self.assertEqual(asset_ref("https://ieeebangalore.org/student-branches/").kind, "page")
        self.assertNotIn("https://ieeebangalore.org/student-branches", leads, "a page: lead_page would read the host's homepage, not it")


if __name__ == "__main__":
    unittest.main()
