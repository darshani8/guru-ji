import asyncio
import hashlib
import importlib.util
import unittest
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.fetch import Retrieval
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.apis import CourtRecordsConnector
from app.internet_intelligence.map.connectors.base import ConnectorContext, ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.connectors.feeds import youtube_feed_url
from app.internet_intelligence.map.connectors.hubs import LinkHubConnector
from app.internet_intelligence.map.connectors.web import LeadPageConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.gate import compare
from app.internet_intelligence.map.incidents import IncidentDesk
from app.internet_intelligence.map.metrics import map_metrics
from app.internet_intelligence.map.pipeline import nominated, regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.search import StaticSearchProvider, hits_from_fixture
from app.internet_intelligence.service import InternetIntelligenceService, InvestigationQuotaExceeded
from app.internet_intelligence.store import IntelligenceStore
from platform_fixtures import principal
from test_intelligence_map_connectors import api

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
INSTITUTION = "bgscet"
PROFILE = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])
HUB_PAGE = '<html><head><title>links</title></head><body><a href="https://www.instagram.com/{account}/">Instagram</a> <a href="https://{site}/">site</a></body></html>'


class PageFetcher:
    """Answers every URL with the same public page, as a hub or a site would."""

    def __init__(self, page):
        self.page = page

    async def retrieve(self, url, **kwargs):
        return Retrieval(url=url, outcome="ok", http_status=200, body=self.page.encode(), content_type="text/html")


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, PROFILE)["entity_id"]
        self.service = MapService(self.store)
        self.manager = principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION)
        self.reader = principal(PrincipalType.FACULTY, college_id=INSTITUTION)

    def account(self, url, relation="unknown"):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id, relation=relation)
        self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="search_snippet", channel="search:t", observed_via="index")
        regrade(self.store, INSTITUTION, [asset_id])
        return asset_id

    def queue(self, asset_id, kind="impersonation_candidate"):
        review_id, outcome = self.store.add_review_item(INSTITUTION, kind=kind, title="check this", asset_id=asset_id, url=self.store.get_asset(INSTITUTION, asset_id)["url"])
        self.assertEqual(outcome, "added")
        return review_id


class QueueStoreTests(Base):
    def test_items_are_queued_once_capped_and_expire(self):
        first = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/1/")
        again = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET (re-found)", url="https://indiankanoon.org/doc/1/")
        self.assertEqual((first[0], again), (first[0], (first[0], "exists")))
        with self.assertRaises(ValueError):
            self.store.add_review_item(INSTITUTION, kind="gossip", title="x")
        with mock.patch("app.internet_intelligence.map.store.MAX_OPEN_REVIEW", 1):
            self.assertEqual(self.store.add_review_item(INSTITUTION, kind="candidate_account", title="another", url="https://www.instagram.com/someone/"), (None, "full"))
        self.assertEqual(self.store.review_counts(INSTITUTION), {"court_record": 1})
        self.assertEqual(self.store.expire_review_items(INSTITUTION, now=(NOW + timedelta(days=400)).isoformat()), 1)
        self.assertEqual(self.store.list_review_items(INSTITUTION), [])
        self.assertEqual(self.store.list_review_items("other", status=None), [], "the queue is per institution")

    def test_a_full_queue_still_takes_what_cannot_wait(self):
        with mock.patch("app.internet_intelligence.map.store.MAX_OPEN_REVIEW", 2):
            for number in range(2):
                self.store.add_review_item(INSTITUTION, kind="dispute", title=f"dispute {number}", url=f"https://www.instagram.com/claimant{number}/")
            self.assertEqual(self.store.add_review_item(INSTITUTION, kind="candidate_account", title="one more", url="https://www.instagram.com/someone/")[1], "full")
            for kind in ("court_record", "impersonation_candidate", "canary_leak", "run_gate"):
                self.assertEqual(self.store.add_review_item(INSTITUTION, kind=kind, title=kind, url=f"https://example.org/{kind}")[1], "added", kind)
            self.assertEqual(self.store.add_review_item(INSTITUTION, kind="dispute", title="urgent", severity="high", url="https://www.instagram.com/x9/")[1], "added")
            # A held re-scoring still reaches a manager, who can publish or discard it.
            stale = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=self.entity_id, relation="official")[0]
            # One live search result: a seed counts as verified only on the map's own evidence.
            self.store.add_evidence(INSTITUTION, asset_id=stale, kind="search_snippet", channel="search:t", observed_via="index")
            self.store.set_grade(INSTITUTION, stale, grade="B", reasons=["old rule"], scorer_version="grader-0")
            self.store.add_gold(INSTITUTION, asset_key="instagram:bgscet_engg_coll", platform="instagram", split="seed", expected_min_grade="B")
            self.assertFalse(self.service.rescore(self.manager, INSTITUTION)["passed"])
        self.assertEqual(len(self.store.list_review_items(INSTITUTION, kind="run_gate")), 2)

    def test_an_item_that_expired_undecided_is_asked_again(self):
        account = self.account("https://www.instagram.com/bgscet.official/")
        first = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="calls itself official", asset_id=account, url="https://www.instagram.com/bgscet.official/")
        court = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/7/")
        later = (datetime.now(timezone.utc) + timedelta(days=91)).isoformat()
        self.assertEqual(self.store.expire_review_items(INSTITUTION, now=later), 2)
        again = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="still calls itself official", detail="found again", asset_id=account, url="https://www.instagram.com/bgscet.official/", run_id="imrn-2", now=later)
        self.assertEqual(again, (first[0], "reopened"))
        self.assertEqual(self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/7/", now=later), (court[0], "reopened"))
        item = self.store.get_review_item(INSTITUTION, first[0])
        self.assertEqual((item["status"], item["title"], item["detail"], item["run_id"]), ("open", "still calls itself official", "found again", "imrn-2"))
        self.assertGreater(item["expires_at"], later, "it gets a fresh lease on the queue")
        # A decided item is never asked again.
        self.service.decide(self.manager, INSTITUTION, first[0], decision="dismiss")
        self.assertEqual(self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="again", asset_id=account, url="https://www.instagram.com/bgscet.official/")[1], "exists")

    def test_one_account_is_one_item_however_its_url_is_spelled(self):
        account = self.account("https://www.instagram.com/bgscet.official/")
        first, _ = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="calls itself official", asset_id=account, url="https://www.instagram.com/bgscet.official/")
        self.service.decide(self.manager, INSTITUTION, first, decision="dismiss", note="a fan page")
        for url in ("https://instagram.com/bgscet.official", "https://www.instagram.com/bgscet.official/?hl=en", "https://m.instagram.com/BGSCET.official/"):
            self.assertEqual(self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="calls itself official", asset_id=account, url=url), (first, "exists"), url)
        court, _ = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/7/")
        self.assertEqual(self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/7?utm_source=feed"), (court, "exists"))
        self.assertEqual(self.store.list_review_items(INSTITUTION), [self.store.get_review_item(INSTITUTION, court)])
        # An item queued under the old fingerprint (asset and raw URL) is still the same item.
        other = self.account("https://www.instagram.com/bgscet_admissions/")
        legacy = hashlib.sha256(f"dispute|{other}|https://www.instagram.com/bgscet_admissions/|".encode()).hexdigest()
        old, _ = self.store.add_review_item(INSTITUTION, kind="dispute", title="two official accounts", asset_id=other, url="https://www.instagram.com/bgscet_admissions/", fingerprint=legacy)
        self.assertEqual(self.store.add_review_item(INSTITUTION, kind="dispute", title="two official accounts", asset_id=other, url="https://www.instagram.com/bgscet_admissions/"), (old, "exists"))


class DecisionTests(Base):
    def test_confirm_reject_and_the_rules_around_them(self):
        confirmed = self.account("https://www.instagram.com/bgscet_mba/")
        rejected = self.account("https://www.instagram.com/bgscet.official/")
        self.store.upsert_source(INSTITUTION, connector="lead_page", target="https://www.instagram.com/bgscet.official/", asset_id=rejected, origin="lead", work_class="explore")
        first, second = self.queue(confirmed), self.queue(rejected)
        with self.assertRaises(PermissionError):
            self.service.review_items(self.reader, INSTITUTION)
        with self.assertRaises(PermissionError):
            self.service.decide(self.reader, INSTITUTION, first, decision="confirm")
        with self.assertRaisesRegex(ValueError, "takes one of"):
            self.service.decide(self.manager, INSTITUTION, first, decision="publish")
        result = self.service.decide(self.manager, INSTITUTION, first, decision="confirm", note="the MBA office's account", relation="official")
        asset = self.store.get_asset(INSTITUTION, confirmed)
        self.assertEqual((asset["grade"], asset["relation"]), ("B", "official"), "a reviewer's word is B, never A")
        self.assertEqual(result["changes"][0]["to"], "B")
        with self.assertRaisesRegex(ValueError, "already decided"):
            self.service.decide(self.manager, INSTITUTION, first, decision="reject")
        result = self.service.decide(self.manager, INSTITUTION, second, decision="impersonation", note="not ours; copies our logo")
        self.assertEqual(self.store.get_asset(INSTITUTION, rejected)["grade"], "D")
        self.assertEqual(result["incident"]["kind"], "impersonation_confirmed")
        self.assertEqual(result["sources_pruned"], 1)
        evidence = self.store.list_evidence(INSTITUTION, asset_id=rejected)
        self.assertEqual((evidence[-1]["kind"], evidence[-1]["observed_via"], evidence[-1]["channel"]), ("impersonation", "reviewer", f"reviewer:{self.manager.principal_id}"))
        with self.assertRaises(KeyError):
            self.service.decide(self.manager, INSTITUTION, "irev-missing", decision="confirm")

    def test_a_personal_account_leaves_no_trace(self):
        person = self.account("https://www.instagram.com/rahul.k/")
        self.store.upsert_source(INSTITUTION, connector="youtube", target=person, asset_id=person, origin="recurring")
        review_id = self.queue(person, kind="candidate_account")
        self.service.decide(self.manager, INSTITUTION, review_id, decision="personal", note="a student's own account")
        self.assertIsNone(self.store.get_asset(INSTITUTION, person))
        self.assertEqual(self.store.list_evidence(INSTITUTION, asset_id=person), [])
        self.assertEqual(self.store.list_sources(INSTITUTION, connector="youtube"), [])
        item = self.store.get_review_item(INSTITUTION, review_id)
        self.assertEqual((item["url"], item["asset_id"], item["decision"]), ("", None, "personal"))
        self.assertNotIn("rahul", item["title"])
        self.assertTrue(self.store.is_suppressed(INSTITUTION, "instagram:rahul.k"), "it can never be added again")

    def test_court_records_are_acknowledged_not_graded(self):
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/1/")
        with self.assertRaisesRegex(ValueError, "acknowledge"):
            self.service.decide(self.manager, INSTITUTION, review_id, decision="confirm")
        self.assertEqual(self.service.decide(self.manager, INSTITUTION, review_id, decision="acknowledge")["kind"], "court_record")


class RejectionTests(Base):
    def test_a_rejected_lookalike_takes_back_the_grade_it_lent(self):
        # An outdated AICTE listing names a look-alike domain: it anchors (A), and so does its footer account.
        domain, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://bgscet-edu.in/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=domain, kind="directory_record", detail="authority:aicte listing", channel="directory:aicte", observed_via="live")
        footer, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_edu/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=footer, kind="official_link", detail="A:footer", source_url="https://bgscet-edu.in/", source_asset_id=domain, channel="site:bgscet-edu.in", observed_via="live")
        self.store.upsert_source(INSTITUTION, connector="lead_page", target="https://bgscet-edu.in/partners", origin="lead", work_class="explore")
        regrade(self.store, INSTITUTION, [domain, footer])
        self.assertEqual((self.store.get_asset(INSTITUTION, domain)["grade"], self.store.get_asset(INSTITUTION, footer)["grade"]), ("A", "A"))
        self.assertTrue(nominated(self.store, INSTITUTION, domain))
        result = self.service.decide(self.manager, INSTITUTION, self.queue(domain), decision="lookalike", note="registered by someone else")
        self.assertEqual((result["links_refuted"], result["sources_pruned"]), (1, 1))
        self.assertEqual(self.store.get_asset(INSTITUTION, domain)["grade"], "D")
        self.assertEqual(self.store.get_asset(INSTITUTION, footer)["grade"], "unrated", "no A, and no A-arch either")
        self.assertFalse(nominated(self.store, INSTITUTION, domain), "a reviewer's later rejection withdraws the regulator's listing")
        self.assertEqual([source["status"] for source in self.store.list_sources(INSTITUTION, connector="lead_page")], ["pruned"])
        self.assertNotIn(footer, {asset["asset_id"] for asset in self.service.assets(self.reader, INSTITUTION)})
        # A link the refuted site gives afterwards counts no more than the old one.
        self.store.add_evidence(INSTITUTION, asset_id=footer, kind="official_link", detail="A:footer", source_url="https://bgscet-edu.in/", source_asset_id=domain, channel="site:bgscet-edu.in", observed_via="live")
        regrade(self.store, INSTITUTION, [footer])
        self.assertEqual(self.store.get_asset(INSTITUTION, footer)["grade"], "unrated")
        # Only a person naming it again (a later confirmation) makes it the institution's again.
        self.store.add_evidence(INSTITUTION, asset_id=domain, kind="reviewer_confirm", channel="reviewer:x", observed_via="reviewer")
        self.assertTrue(nominated(self.store, INSTITUTION, domain))

    def test_a_hub_rejected_as_an_impostor_takes_back_what_it_listed(self):
        home = self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")["asset_id"]
        hub, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://linktr.ee/bgscet_links"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=hub, kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=home, channel="site:bgscet.ac.in", observed_via="live")
        listed, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.facebook.com/bgscet.alumni/"), entity_id=self.entity_id, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=listed, kind="hub_link", detail="A:hub", source_url="https://linktr.ee/bgscet_links", source_asset_id=hub, channel="hub:bgscet_links", observed_via="live")
        regrade(self.store, INSTITUTION, [home, hub, listed])
        self.assertEqual((self.store.get_asset(INSTITUTION, hub)["grade"], self.store.get_asset(INSTITUTION, listed)["grade"]), ("A", "B"))
        self.service.decide(self.manager, INSTITUTION, self.queue(hub), decision="impersonation", note="not ours")
        self.assertEqual(self.store.get_asset(INSTITUTION, listed)["grade"], "unrated")
        [refuted] = [row for row in self.store.list_evidence(INSTITUTION, asset_id=listed) if row["kind"] == "source_refuted"]
        self.assertEqual((refuted["polarity"], refuted["source_asset_id"], refuted["observed_via"]), ("refutes", hub, "reviewer"))
        self.assertEqual(self.store.get_asset(INSTITUTION, home)["grade"], "A", "the site that linked the hub is not touched")

    def test_the_leads_a_rejected_hub_gave_go_with_it(self):
        hub = self.account("https://linktr.ee/bgs_fake")
        self.store.add_evidence(INSTITUTION, asset_id=hub, kind="community_record", channel="wikidata", observed_via="index")
        regrade(self.store, INSTITUTION, [hub])
        engine = MapEngine(self.store, ConnectorRegistry([LinkHubConnector(active=True)]), fetcher=PageFetcher(HUB_PAGE.format(account="bgs_fake_ig", site="bgs-fake-site.com")), clock=lambda: NOW)
        asyncio.run(engine.tick(INSTITUTION))
        [lead] = self.store.list_sources(INSTITUTION, connector="lead_page")
        self.assertEqual((lead["target"], lead["parent_asset_id"]), ("https://bgs-fake-site.com/", hub), "a lead remembers the hub it was found on")
        listed = self.store.find_asset(INSTITUTION, "instagram:bgs_fake_ig")["asset_id"]
        self.store.add_evidence(INSTITUTION, asset_id=listed, kind="search_snippet", channel="search:tavily", observed_via="index")
        regrade(self.store, INSTITUTION, [listed])
        self.assertEqual(self.store.get_asset(INSTITUTION, listed)["grade"], "B", "the hub and a search agree")
        result = self.service.decide(self.manager, INSTITUTION, self.queue(hub), decision="lookalike", note="not ours")
        self.assertEqual(result["sources_pruned"], 2, "the hub's own watch and the lead found on it")
        self.assertEqual(self.store.get_source(INSTITUTION, lead["source_id"])["status"], "pruned")
        self.assertEqual(self.store.get_asset(INSTITUTION, listed)["grade"], "C", "only the search is left")

    def test_a_rejected_site_loses_its_leads_under_every_spelling_and_vouches_no_more(self):
        site, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.bgscet-admissions.example/"), entity_id=None)
        self.store.add_evidence(INSTITUTION, asset_id=site, kind="backlink", channel="lead", observed_via="live")
        spellings = ("https://www.bgscet-admissions.example/", "https://www.bgscet-admissions.example/apply", "http://bgscet-admissions.example/fees", "https://www.bgscet-admissions.example")
        for target in (*spellings, "https://bgscet-admissions.example.org/"):
            self.store.upsert_source(INSTITUTION, connector="lead_page", target=target, origin="lead", work_class="explore", hops=1)
        result = self.service.decide(self.manager, INSTITUTION, self.queue(site), decision="impersonation", note="takes fees in our name")
        self.assertEqual(result["sources_pruned"], len(spellings))
        self.assertEqual({source["target"]: source["status"] for source in self.store.list_sources(INSTITUTION)}, {**{target: "pruned" for target in spellings}, "https://bgscet-admissions.example.org/": "active"})
        # A lead that slipped through finds the site refuted, before fetching or recording anything.
        account = self.account("https://www.instagram.com/bgscet_admission_desk/")
        page = (
            '<html><head><title>BGS College of Engineering and Technology - Admissions</title></head><body><header><a href="https://www.instagram.com/bgscet_admission_desk/">Instagram</a></header>'
            "<p>BGS College of Engineering and Technology, Bengaluru. BGSCET admissions 2026.</p></body></html>"
        )
        context = ConnectorContext(self.store, INSTITUTION, "run-2", NOW, fetcher=PageFetcher(page))
        for parked in (False, True):
            if parked:  # a grade parked under an older one still leaves the reviewer's rejection standing
                self.store.set_grade(INSTITUTION, site, grade="C", reasons=["older rule"], scorer_version="grader-0")
            outcome = asyncio.run(LeadPageConnector().run({"target": "https://www.bgscet-admissions.example/", "hops": 1}, context))
            self.assertEqual((outcome.outcome, outcome.prune), ("refuted", True))
        self.assertEqual(self.store.get_asset(INSTITUTION, account)["grade"], "C")
        self.assertEqual([row["kind"] for row in self.store.list_evidence(INSTITUTION, asset_id=account)], ["search_snippet"])

    def test_forgetting_a_personal_hub_leaves_nothing_that_names_the_person(self):
        hub = self.account("https://linktr.ee/rahul.k")
        engine = MapEngine(self.store, ConnectorRegistry([LinkHubConnector(active=True)]), fetcher=PageFetcher(HUB_PAGE.format(account="rahul.k.photos", site="rahulk-portfolio.example")), clock=lambda: NOW)
        asyncio.run(engine.tick(INSTITUTION))
        listed = self.store.find_asset(INSTITUTION, "instagram:rahul.k.photos")
        self.assertEqual((listed["note"], listed["grade"]), ("listed on rahul.k", "C"))
        self.assertEqual(len(self.store.list_sources(INSTITUTION)), 2, "the hub's watch and the lead found on it")
        self.store.record_fetch(INSTITUTION, "https://linktr.ee/rahul.k", outcome="ok", etag='"v1"', last_modified=None, content_sha256=None)
        self.service.decide(self.manager, INSTITUTION, self.queue(hub), decision="personal", note="a student's own page")
        self.assertIsNone(self.store.get_asset(INSTITUTION, hub))
        for row in self.store.list_evidence(INSTITUTION):
            self.assertNotIn("rahul.k", f"{row['source_url']} {row['channel']} {row['detail']}")
        self.assertEqual(self.store.list_sources(INSTITUTION), [])
        listed = self.store.get_asset(INSTITUTION, listed["asset_id"])
        self.assertEqual((listed["note"], listed["grade"]), ("", "unrated"), "regraded without the hub's link")
        self.assertIsNone(self.store.fetch_state(INSTITUTION, "https://linktr.ee/rahul.k"))
        self.assertNotIn("rahul", " ".join(f"{item['title']} {item['url']}" for item in self.store.list_review_items(INSTITUTION, status=None)))
        # A personal YouTube channel takes its feed's validator with it.
        channel = self.account("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")
        feed = youtube_feed_url("UCabcdefghijklmnopqrstuv")
        self.store.record_fetch(INSTITUTION, feed, outcome="ok", etag='"v1"', last_modified=None, content_sha256=None)
        self.service.decide(self.manager, INSTITUTION, self.queue(channel), decision="personal")
        self.assertIsNone(self.store.fetch_state(INSTITUTION, feed))


class ConcurrentDecisionTests(Base):
    """Two managers with the same item open: only the decision whose claim wins has any effect."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.service = MapService(self.store, desk=IncidentDesk(self.store, recipients=lambda _: ["it@bgscet.ac.in"], notify=lambda *args: self.sent.append(args[2])))
        self.other = principal(PrincipalType.PRINCIPAL, "manager-2", college_id=INSTITUTION)
        self.target = self.account("https://www.instagram.com/bgscet_mba/")
        self.store.upsert_source(INSTITUTION, connector="lead_page", target="https://www.instagram.com/bgscet_mba/", asset_id=self.target, origin="lead", work_class="explore")
        self.review_id = self.queue(self.target)
        # What the second manager's page loaded while the item was still open.
        self.stale = self.store.get_review_item(INSTITUTION, self.review_id)

    def late_decision(self, decision):
        with mock.patch.object(self.store, "get_review_item", return_value=self.stale), self.assertRaisesRegex(ValueError, "someone else, or expired"):
            self.service.decide(self.other, INSTITUTION, self.review_id, decision=decision, note="fake")

    def assert_untouched(self, grade):
        self.assertEqual(self.store.get_asset(INSTITUTION, self.target)["grade"], grade)
        self.assertEqual({row["kind"] for row in self.store.list_evidence(INSTITUTION, asset_id=self.target)} & {"impersonation", "lookalike", "reviewer_reject"}, set())
        self.assertEqual([source["status"] for source in self.store.list_sources(INSTITUTION)], ["active"])
        self.assertEqual((self.store.list_incidents(INSTITUTION), self.sent), ([], []))
        self.assertFalse(self.store.is_suppressed(INSTITUTION, "instagram:bgscet_mba"))

    def test_the_second_decision_changes_nothing(self):
        self.service.decide(self.manager, INSTITUTION, self.review_id, decision="confirm", relation="official")
        for decision in ("impersonation", "personal"):
            self.late_decision(decision)
        self.assert_untouched("B")
        item = self.store.get_review_item(INSTITUTION, self.review_id)
        self.assertEqual((item["decision"], item["decided_by"], item["url"]), ("confirm", self.manager.principal_id, "https://www.instagram.com/bgscet_mba/"))

    def test_an_item_that_expired_meanwhile_changes_nothing(self):
        self.store.expire_review_items(INSTITUTION, now=(datetime.now(timezone.utc) + timedelta(days=400)).isoformat())
        self.late_decision("impersonation")
        self.assert_untouched("C")
        self.assertEqual(self.store.get_review_item(INSTITUTION, self.review_id)["status"], "expired")

    def test_a_decision_that_fails_part_way_leaves_the_item_open(self):
        for decision, broken in (("impersonation", "prune_sources_for"), ("personal", "forget_asset")):
            with mock.patch.object(self.store, broken, side_effect=RuntimeError("the database went away")), self.assertRaises(RuntimeError):
                self.service.decide(self.manager, INSTITUTION, self.review_id, decision=decision)
            self.assert_untouched("C")
            item = self.store.get_review_item(INSTITUTION, self.review_id)
            self.assertEqual((item["status"], item["url"], item["asset_id"]), ("open", "https://www.instagram.com/bgscet_mba/", self.target))
        # Tried again, the forgetting and the item's redaction commit together.
        self.service.decide(self.manager, INSTITUTION, self.review_id, decision="personal")
        item = self.store.get_review_item(INSTITUTION, self.review_id)
        self.assertEqual((item["status"], item["url"], item["asset_id"]), ("decided", "", None))
        self.assertIsNone(self.store.get_asset(INSTITUTION, self.target))


class GateTests(Base):
    def test_a_known_lookalike_that_reaches_b_is_set_back_and_reported(self):
        self.store.add_gold(INSTITUTION, asset_key="web:bgsgroupofinstitutions.com", platform="website", split="canary", entity_name="BGS Group of Institutions", relation="third_party", expected_min_grade="D")
        leak, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.bgsgroupofinstitutions.com/"), entity_id=None)
        for channel in ("search:tavily", "wikidata"):
            self.store.add_evidence(INSTITUTION, asset_id=leak, kind="search_snippet" if channel.startswith("search") else "community_record", channel=channel, observed_via="index")

        class Touch:
            name, access_mode, budget_key, max_grade, default_interval = "touch", "public_page", "fetch", "C", 86400

            def enabled(self):
                return True

            def cost(self, source):
                return 0

            async def run(self, source, context):
                return ConnectorResult(outcome="ok", touched={leak}, yield_count=1)

        self.store.upsert_source(INSTITUTION, connector="touch", target="t", due_at=NOW.isoformat())
        result = asyncio.run(MapEngine(self.store, ConnectorRegistry([Touch()]), clock=lambda: NOW).tick(INSTITUTION))
        self.assertEqual(self.store.get_asset(INSTITUTION, leak)["grade"], "D", "two channels made it B; ground truth sets it back")
        self.assertEqual([incident["kind"] for incident in result["incidents"]], ["canary_leak"])
        self.assertEqual(self.store.list_map_runs(INSTITUTION, kind="tick")[0]["gate"], "held")
        self.assertEqual(sorted(item["kind"] for item in self.store.list_review_items(INSTITUTION)), ["canary_leak", "run_gate"])
        self.assertEqual(map_metrics(self.store, INSTITUTION)["canary_leaks"], [])

    def test_a_rescore_that_loses_ground_truth_waits_for_a_person(self):
        stale = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=self.entity_id, relation="official")[0]
        # Graded B under an older rule from one search snippet, which the current rule grades C.
        self.store.set_grade(INSTITUTION, stale, grade="B", reasons=["old rule"], scorer_version="grader-0")
        self.store.add_evidence(INSTITUTION, asset_id=stale, kind="search_snippet", channel="search:t", observed_via="index")
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgscet_engg_coll", platform="instagram", split="seed", expected_min_grade="B")
        held = self.service.rescore(self.manager, INSTITUTION)
        self.assertFalse(held["passed"])
        self.assertIn("seed verification would fall", held["reasons"][0])
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["grade"], "B", "nothing is published while held")
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["proposed_grade"], "C")
        [gate] = self.store.list_review_items(INSTITUTION, kind="run_gate")
        self.service.decide(self.manager, INSTITUTION, gate["review_id"], decision="discard")
        self.assertIsNone(self.store.get_asset(INSTITUTION, stale)["proposed_grade"])
        # With evidence the rule accepts, the same rescore passes and publishes.
        self.store.add_evidence(INSTITUTION, asset_id=stale, kind="reviewer_confirm", channel="reviewer:x", observed_via="reviewer")
        passed = self.service.rescore(self.manager, INSTITUTION)
        self.assertTrue(passed["passed"])
        self.assertEqual(self.store.get_asset(INSTITUTION, stale)["grade"], "B")
        self.assertEqual(self.store.list_map_runs(INSTITUTION, kind="rescore")[0]["gate"], "passed")

    def test_the_comparison(self):
        base = {"canary_leaks": [], "seed_verification_rate": 0.8, "holdout_recall": 0.5}
        self.assertTrue(compare(base, {**base, "seed_verification_rate": 0.79}).passed, "within tolerance")
        self.assertFalse(compare(base, {**base, "holdout_recall": 0.4}).passed)
        self.assertFalse(compare(base, {**base, "canary_leaks": [{"asset_key": "web:x"}]}).passed)
        self.assertTrue(compare({**base, "canary_leaks": [{"asset_key": "web:x"}]}, {**base, "canary_leaks": [{"asset_key": "web:x"}]}).passed, "only new leaks block")


class EngineQueueTests(Base):
    def test_court_records_are_queued_once_across_ticks(self):
        client = api({("api.indiankanoon.org", "/search/"): (200, {"docs": [{"tid": 7, "title": "BGS College of Engineering and Technology v. State", "headline": ""}]})})
        clock = [NOW]
        engine = MapEngine(self.store, ConnectorRegistry([CourtRecordsConnector(api_token="t", active=True, client=client)]), clock=lambda: clock[0])
        first = asyncio.run(engine.tick(INSTITUTION))
        clock[0] = NOW + timedelta(days=31)
        second = asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual((first["counts"]["review"], second["counts"]["review"]), (1, 0), "found again, not asked again")
        [item] = self.store.list_review_items(INSTITUTION)
        self.assertEqual((item["kind"], item["connector"], item["url"]), ("court_record", "court_records", "https://indiankanoon.org/doc/7/"))


CONTROVERSY = [
    {"url": "https://citynews.example.com/abc-protest", "title": "Students protest fee hike at ABC College", "snippet": "Students of ABC College, Bengaluru staged a protest; a complaint was filed.", "published_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    {"url": "https://citynews.example.com/abc-fest", "title": "ABC College fest draws crowds", "snippet": "The annual fest at ABC College, Bengaluru was held.", "published_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
]
ABC = InstitutionProfile("college_a", "ABC College", "Bengaluru", monitoring_enabled=True)


class ReaderAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = IntelligenceStore(":memory:")
        self.service = InternetIntelligenceService(self.store, StaticSearchProvider(hits_from_fixture(CONTROVERSY)), fetcher=None, investigations_per_day=2)
        self.manager = principal(PrincipalType.PRINCIPAL)
        self.reader = principal(PrincipalType.FACULTY)
        self.service.save_profile(self.manager, "college_a", ABC.as_dict())

    async def test_sensitive_findings_reach_managers_first(self):
        managed = await self.service.investigate(self.manager, "college_a", window_days=7)
        self.assertIn("https://citynews.example.com/abc-protest", [item["url"] for item in managed["findings"]])
        read = await self.service.investigate(self.reader, "college_a", window_days=7)
        self.assertEqual([item["url"] for item in read["findings"]], ["https://citynews.example.com/abc-fest"])
        self.assertIn("held_for_review", {warning["code"] for warning in read["warnings"]})
        self.assertEqual(read["review_candidates"], [])
        self.assertNotIn("protest", read["summary"].lower())
        self.assertEqual(len(self.store.list_reports("college_a")), 2)
        self.assertEqual(len(self.store.list_reports("college_a", include_sensitive=False)), 1, "the manager's report mentions the protest")
        kept = {row["url"] for row in self.store.list_documents("college_a", exclude_sensitive=True)}
        self.assertEqual(kept, {"https://citynews.example.com/abc-fest"})

    async def test_each_person_has_a_daily_allowance(self):
        await self.service.investigate(self.reader, "college_a", window_days=7)
        await self.service.investigate(self.reader, "college_a", window_days=7)
        with self.assertRaises(InvestigationQuotaExceeded):
            await self.service.investigate(self.reader, "college_a", window_days=7)
        other = principal(PrincipalType.FACULTY, principal_id="faculty-2")
        await self.service.investigate(other, "college_a", window_days=7)
        self.assertEqual(self.store.investigations_used("college_a", self.reader.principal_id, day=datetime.now(timezone.utc).date().isoformat()), 2)

    async def test_the_digest_hides_sensitive_events_from_readers(self):
        from app.internet_intelligence.monitoring import ContinuousMonitor

        await ContinuousMonitor(self.service, self.store, window_days=7).run_for("college_a")
        self.assertEqual(self.service.digest(self.manager, "college_a", days=1)["total_items"], 2)
        reader_digest = self.service.digest(self.reader, "college_a", days=1)
        self.assertEqual(reader_digest["total_items"], 1)
        self.assertNotIn("protest", reader_digest["summary"].lower())


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class ReviewRouteTests(unittest.TestCase):
    def test_the_queue_routes_are_for_managers(self):
        college = f"rev_college_{uuid4().hex[:8]}"  # a fresh tenant, so the test passes on a reused database too
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "rev-principal", "X-Demo-Role": "principal", "X-Demo-College": college}
        faculty = {**headers, "X-Demo-Principal": "rev-faculty", "X-Demo-Role": "faculty"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        asset_id, _ = store.upsert_asset(college, asset_ref("https://www.instagram.com/rev_college_official/"), entity_id=None)
        review_id, _ = store.add_review_item(college, kind="impersonation_candidate", title="calls itself official", asset_id=asset_id, url="https://www.instagram.com/rev_college_official/")
        with mock.patch.object(platform, "intelligence_map", MapService(store)):
            self.assertEqual(client.get("/v1/intelligence/map/review", headers=faculty).status_code, 403)
            listed = client.get("/v1/intelligence/map/review", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["review_id"] for item in listed.json()["items"]], [review_id])
            self.assertEqual(client.post(f"/v1/intelligence/map/review/{review_id}/decision", headers=headers, json={"decision": "publish"}).status_code, 422)
            self.assertEqual(client.post("/v1/intelligence/map/review/irev-nope/decision", headers=headers, json={"decision": "reject"}).status_code, 404)
            decided = client.post(f"/v1/intelligence/map/review/{review_id}/decision", headers=headers, json={"decision": "reject", "note": "not ours"})
            self.assertEqual(decided.status_code, 200, decided.text)
            self.assertEqual(store.get_asset(college, asset_id)["grade"], "D")
            self.assertEqual(client.post("/v1/intelligence/map/rescore", headers=headers, json={}).json()["passed"], True)
            events = {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)}
            self.assertTrue({"intelligence.map.review_decision", "intelligence.map.rescore"} <= events)

    def test_readers_get_only_kept_mentions(self):
        client = TestClient(app)
        faculty = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "men-faculty", "X-Demo-Role": "faculty", "X-Demo-College": "men_college"}
        self.assertEqual(client.get("/v1/intelligence/mentions?status=review", headers=faculty).status_code, 403)
        ok = client.get("/v1/intelligence/mentions", headers=faculty)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["sensitive_hidden"])


if __name__ == "__main__":
    unittest.main()
