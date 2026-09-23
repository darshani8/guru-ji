"""People's names are taken out of what the internet intelligence stores (India's DPDP Act)."""

import asyncio
import json
import unittest
from datetime import timedelta

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import CourtRecordsConnector
from app.internet_intelligence.map.connectors.base import ConnectorRegistry
from app.internet_intelligence.map.connectors.search import SearchConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.export import export_rows, export_tsv
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.seed import import_seed, load_default_seed, parse_sweep
from app.internet_intelligence.map.store import FREE_TEXT_EVIDENCE, MapStore
from app.internet_intelligence.redaction import names_of, strip_person_names
from app.internet_intelligence.search import SearchHit, StaticSearchProvider, hits_from_fixture
from app.internet_intelligence.service import InternetIntelligenceService
from app.internet_intelligence.store import IntelligenceStore
from platform_fixtures import principal
from test_intelligence_map import sweep
from test_intelligence_map_connectors import INSTITUTION, Fixture, api
from test_intelligence_map_engine import NOW, PROFILE
from test_internet_intelligence import NOW as P0_NOW
from test_internet_intelligence import PROFILE as P0_PROFILE

SWAMIJI = "Sri Sri Sri Dr. Nirmalanandanatha Mahaswamiji"
KEEP = (SWAMIJI, "BGS College of Engineering and Technology", "BGS", "Adichunchanagiri")


def strip(text: str) -> str:
    return strip_person_names(text, keep=KEEP)


class PatternTests(unittest.TestCase):
    def test_honorific_led_names_lose_the_honorific_too(self):
        self.assertEqual(strip('Display name is now "Dr. Ravi Kumar G K" (the principal).'), 'Display name is now "[name]" (the principal).')
        self.assertEqual(strip("Prof. Suresh Babu inaugurated the lab"), "[name] inaugurated the lab")
        self.assertEqual(strip("Smt. Lakshmi Devi and Sri Ramesh attended"), "[name] and [name] attended")
        self.assertEqual(strip("Mr. Anil Kumar's office; Ms. Asha Rao; Mrs. Rekha; Shri K. V. Gowda; Kum. Priya S. spoke"), "[name]'s office; [name]; [name]; [name]; [name] spoke")

    def test_relations_take_the_name_before_and_after(self):
        self.assertEqual(strip("Ramesh Kumar S/O Late Gowda, resident of Bengaluru"), "[name] S/O Late [name], resident of Bengaluru")
        self.assertEqual(strip("Lakshmi D/o Ramaiah and Suma W/O Ravi"), "[name] D/o [name] and [name] W/O [name]")
        self.assertEqual(strip("RAMESH KUMAR S/O LATE GOWDA"), "[name] S/O LATE [name]", "court papers write names in capitals beside S/O")

    def test_case_parties_that_are_people_are_removed_and_institutions_kept(self):
        self.assertEqual(strip("Ramesh Kumar S/O Late Gowda vs BGS College of Engineering and Technology on 3 March, 2020"), "[name] vs BGS College of Engineering and Technology on 3 March, 2020")
        self.assertEqual(strip("Ramesh Kumar And Ors vs Bgs College Of Engineering"), "[name] And Ors vs Bgs College Of Engineering")
        self.assertEqual(strip("K. Ramesh versus Union Of India"), "[name] versus Union Of India")
        self.assertEqual(strip("Smt. Lakshmi v. The Principal, Some College"), "[name] v. The Principal, Some College")
        for institutional in ("State v. BGS College of Engineering and Technology", "Adichunchanagiri Shikshana Trust vs State of Karnataka", "M/S Ravi Traders vs Union Of India", "India vs Australia"):
            self.assertEqual(strip(institutional), institutional)

    def test_names_beside_a_role_word(self):
        self.assertEqual(strip("Review by Anil Kumar, 2nd year CSE"), "Review by [name], 2nd year CSE")
        self.assertEqual(strip("Posted by Anil Kumar on 3 March"), "Posted by [name] on 3 March")
        self.assertEqual(strip("Written by Asha Rao"), "Written by [name]")
        self.assertEqual(strip("Principal Ravi Kumar addressed the students"), "Principal [name] addressed the students")
        self.assertEqual(strip("Ravi Kumar, Principal"), "[name], Principal")
        self.assertEqual(strip("Happy Birthday Ravi Kumar, student of CSE"), "Happy Birthday [name], student of CSE")
        self.assertEqual(strip("The petitioner Ramesh Kumar and the respondent Suresh Gowda appeared"), "The petitioner [name] and the respondent [name] appeared")
        self.assertEqual(strip("Asha Rao (HOD) and Kiran Shetty - alumnus"), "[name] (HOD) and [name] - alumnus")

    def test_a_display_name_that_opens_a_snippet_and_introduces_a_handle(self):
        self.assertEqual(strip("Priya Sharma (@bgscet_cse) • Instagram photos and videos"), "[name] (@bgscet_cse) • Instagram photos and videos")
        self.assertEqual(strip("tavily: Priya Sharma (@bgscet_cse)"), "tavily: [name] (@bgscet_cse)")
        self.assertEqual(strip("BGSCET Official (@bgscet.official) • Instagram"), "BGSCET Official (@bgscet.official) • Instagram")

    def test_kept_names_are_never_touched(self):
        for text in (
            f"{SWAMIJI} blessed the graduates",
            f"{SWAMIJI.upper()} BLESSED THEM",
            "Official Account of Sri Nirmalanandanatha Swamiji",  # made only of kept names' words
            "Sri Adichunchanagiri Mahasamsthana Math", "Principal, BGS", "Review by BGS College of Engineering and Technology, 2nd year",
        ):
            self.assertEqual(strip(text), text)
        self.assertEqual(strip_person_names(f"{SWAMIJI} blessed them"), "[name] blessed them", "without keep the Swamiji is a person like anyone")
        self.assertEqual(strip(f"{SWAMIJI} blessed Dr. Ravi Kumar"), f"{SWAMIJI} blessed [name]")
        self.assertEqual(names_of([{"name": "BGSCET", "names": ["BGS College"]}, {"name": "Math"}]), ["BGSCET", "BGS College", "Math"])

    def test_plain_institutional_text_is_unchanged(self):
        for text in (
            "BGS College of Engineering and Technology, Mahalakshmipuram, Bengaluru.", "Sri Jayachamarajendra College of Engineering", "Sri Siddhartha Institute of Technology",
            "Sri Venkateshwara Educational and Charitable Trust", "Dr. B. R. Ambedkar Institute of Technology", "Sri Lanka tour", "Sri Rama Navami celebrations", "Jai Sri Gurudev",
            "Sri Kshetra Adichunchanagiri (2012 film)", "Faculty Development Programme on AI", "Computer Science and Engineering, 2nd year", "Principal, SJBIT", "Duplicate principal channel",
            "ABC College Bengaluru: principal arrested, the college is closed.", "claimed A: Official website. Lists no social links.", "Student Welfare Officer", "Alumni Association Meet 2025",
            "", "all lower case text about ravi kumar, a student",
        ):
            self.assertEqual(strip(text), text)

    def test_links_handles_addresses_and_capitals_are_left_alone(self):
        for text in (
            "https://www.youtube.com/@Dr.NNS_Jaisrigurudev", "@PRINCIPAL_BGSCET: 321 subs", "Mail ravi.kumar@bgscet.ac.in", "WRITTEN BY ANIL KUMAR", "Dr.NNS_Jaisrigurudev channel",
            "see https://bgscet.ac.in/principal/Ravi-Kumar",
        ):
            self.assertEqual(strip(text), text)

    def test_scrubbing_twice_changes_nothing_more(self):
        for text in ("Ramesh Kumar S/O Late Gowda vs BGS College", "Review by Anil Kumar, 2nd year CSE", 'Now "Dr. Ravi Kumar G K" (the principal)', "Priya Sharma (@x) • Instagram"):
            once = strip(text)
            self.assertEqual(strip(once), once)

    def test_the_bundled_sweep_names_no_principal_and_keeps_the_swamiji(self):
        rows, lookalikes = load_default_seed()
        self.assertFalse([row for row in rows if "Ravi Kumar" in row.note or "Ravi Kumar" in row.label])
        store = MapStore(":memory:", suppression_key=b"test-key")
        import_seed(store, "bgscet", rows, lookalikes=lookalikes, holdout_percent=0)
        notes = [asset["note"] for asset in store.iter_assets("bgscet")]
        self.assertTrue(any("Official Account of Sri Nirmalanandanatha Swamiji" in note for note in notes), "the Swamiji's names are the map's entities")
        self.assertTrue(any(SWAMIJI in entity["name"] for entity in store.list_entities("bgscet")))


class SearchWritePathTests(Fixture):
    async def test_search_titles_are_stored_without_the_names_in_them(self):
        hits = (SearchHit(url="https://www.instagram.com/bgscet.official/", title="Priya Sharma (@bgscet.official) • Instagram photos and videos", snippet="Official page of BGS College of Engineering and Technology, Bengaluru"),)
        official, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=official, kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=self.domain_id, channel="site:bgscet.ac.in")
        regrade(self.store, INSTITUTION, [official])
        result = await SearchConnector(active=True).run({"target": f"{self.entity_id}|instagram.com"}, self.context(search=StaticSearchProvider(hits)))
        asset = self.store.find_asset(INSTITUTION, "instagram:bgscet.official")
        [snippet] = [item for item in self.store.list_evidence(INSTITUTION, asset_id=asset["asset_id"]) if item["kind"] == "search_snippet"]
        self.assertEqual(snippet["detail"], "static: [name] (@bgscet.official) • Instagram photos and videos")
        [review] = result.review
        self.assertEqual(review["kind"], "impersonation_candidate")
        self.assertNotIn("Priya", review["detail"])
        self.assertIn("BGS College of Engineering and Technology already has", review["detail"], "the entity's own name stays")
        regrade(self.store, INSTITUTION, [asset["asset_id"]])
        self.assertNotIn("Priya", "; ".join(self.store.find_asset(INSTITUTION, "instagram:bgscet.official")["grade_reasons"]))


class CourtWritePathTests(unittest.TestCase):
    def test_court_records_reach_the_review_queue_without_the_litigants(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        sync_profile(store, PROFILE)
        doc = {
            "tid": 9, "title": "Ramesh Kumar S/O Late Gowda vs <b>BGS College of Engineering and Technology</b> on 3 March, 2020",
            "headline": "the petitioner Sri Ramesh Kumar, a student of BGS College of Engineering and Technology, sought his marks card", "docsource": "Karnataka High Court", "publishdate": "2020-03-03",
        }
        client = api({("api.indiankanoon.org", "/search/"): (200, {"docs": [doc]})})
        engine = MapEngine(store, ConnectorRegistry([CourtRecordsConnector(api_token="t", active=True, client=client)]), clock=lambda: NOW)
        asyncio.run(engine.tick(INSTITUTION))
        [item] = store.list_review_items(INSTITUTION, kind="court_record")
        self.assertEqual(item["title"], "[name] vs BGS College of Engineering and Technology on 3 March, 2020")
        self.assertEqual(item["detail"], "Karnataka High Court 2020-03-03: the petitioner [name], a student of BGS College of Engineering and Technology, sought his marks card")
        self.assertEqual(item["url"], "https://indiankanoon.org/doc/9/")


class SeedWritePathTests(unittest.TestCase):
    def test_seed_notes_claims_and_the_export_carry_no_names(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        rows = parse_sweep(sweep(
            f"Mutt\t{SWAMIJI}\trole\t\t{SWAMIJI}\thttps://x.com/Jaisrigurudev\tB\tofficial\tBio: 72nd Pontiff.",
            'BGSCET\tBGS College of Engineering and Technology\tinstitution\t\tBGS College of Engineering and Technology\thttps://www.instagram.com/bgscet_engg_coll/\tA\tofficial\t'
            f'Display name is now "Dr. Anand Rao" (the principal). Tagged by {SWAMIJI}.',
        ))
        import_seed(store, "bgscet", rows, holdout_percent=0)
        regrade(store, "bgscet")
        asset = store.find_asset("bgscet", "instagram:bgscet_engg_coll")
        expected = f'Display name is now "[name]" (the principal). Tagged by {SWAMIJI}.'
        self.assertEqual(asset["note"], expected)
        [claim] = [item for item in store.list_evidence("bgscet", asset_id=asset["asset_id"]) if item["kind"] == "imported_claim"]
        self.assertEqual(claim["detail"], f"claimed A: {expected}")
        [row] = [row for row in export_rows(store, "bgscet") if row["url"] == asset["url"]]
        self.assertIn(expected, row["note"])
        self.assertNotIn("Anand", export_tsv(store, "bgscet"))
        self.assertIn(SWAMIJI, export_tsv(store, "bgscet"))


class StoreBackstopTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.store.upsert_entity("bgscet", name=SWAMIJI, kind="role")
        self.asset_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=None)

    def detail(self, kind, text, **extra):
        evidence_id = self.store.add_evidence("bgscet", asset_id=self.asset_id, kind=kind, detail=text, **extra)
        return next(item["detail"] for item in self.store.list_evidence("bgscet", asset_id=self.asset_id) if item["evidence_id"] == evidence_id)

    def test_free_text_evidence_is_scrubbed_and_the_entities_names_kept(self):
        self.assertEqual(self.detail("reviewer_confirm", f"Confirmed with {SWAMIJI}'s office by Prof. Suresh Babu", observed_via="reviewer"), f"Confirmed with {SWAMIJI}'s office by [name]")
        self.assertEqual(self.detail("search_snippet", "brave: Review by Anil Kumar, 2nd year CSE", observed_via="index"), "brave: Review by [name], 2nd year CSE")

    def test_details_the_grader_parses_are_never_touched(self):
        self.assertEqual(self.detail("hub_link", "B:Principal Ravi Kumar"), "B:Principal Ravi Kumar")
        self.assertEqual(self.detail("official_link", "A:Dr. Ravi Kumar"), "A:Dr. Ravi Kumar")
        parsed = {"official_link", "hub_link", "integrity", "liveness", "subdomain", "directory_record", "configured_domain", "owner_claim", "anchor_lost", "registry_record", "archive_capture"}
        self.assertFalse(FREE_TEXT_EVIDENCE & parsed)

    def test_review_items_are_scrubbed_and_keep_their_fingerprint(self):
        title = "Ramesh Kumar S/O Late Gowda vs State"
        review_id, outcome = self.store.add_review_item("bgscet", kind="court_record", title=title, detail="Review by Anil Kumar, 2nd year CSE")
        item = self.store.get_review_item("bgscet", review_id)
        self.assertEqual((outcome, item["title"], item["detail"]), ("added", "[name] vs State", "Review by [name], 2nd year CSE"))
        self.assertEqual(self.store.add_review_item("bgscet", kind="court_record", title=title)[1], "exists", "the same item found again is still the same item")


class P0WritePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_and_document_excerpts_carry_no_names(self):
        store = IntelligenceStore(":memory:")
        hits = hits_from_fixture([{
            "url": "https://citynews.example.com/education/abc-college-fest", "title": "ABC College fest draws 2,000 students",
            "snippet": "Principal Ravi Kumar opened the annual fest at ABC College, Bengaluru. Review by Anil Kumar, 2nd year MBA: the ABC College Bangalore fest was well run.",
            "published_at": (P0_NOW - timedelta(days=2)).isoformat(),
        }])
        service = InternetIntelligenceService(store, StaticSearchProvider(hits), fetcher=None)
        manager = principal(PrincipalType.PRINCIPAL)
        service.save_profile(manager, "college_a", P0_PROFILE.as_dict())
        report = await service.investigate(manager, "college_a", window_days=7)
        expected = "Principal [name] opened the annual fest at ABC College, Bengaluru. Review by [name], 2nd year MBA: the ABC College Bangalore fest was well run."
        self.assertEqual([item["excerpt"] for item in report["findings"]], [expected])
        self.assertEqual([item["excerpt"] for item in store.list_documents("college_a")], [expected])
        saved = store.backend.fetchone("SELECT findings_json FROM intelligence_reports WHERE report_id = ?", (report["report_id"],))
        self.assertEqual([item["excerpt"] for item in json.loads(saved["findings_json"])], [expected])


if __name__ == "__main__":
    unittest.main()
