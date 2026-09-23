"""Mapping the Math is not speaking for it: whose word settles what the map says about an entity."""

import asyncio
import importlib.util
import unittest
from unittest import mock

from app.config.settings import AppSettings
from app.domain.audit import AuditOutcome
from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.seed import DEFAULT_LOOKALIKES, DEFAULT_SWEEP
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from platform_fixtures import principal
from test_intelligence_map_verification import fetcher_for

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

INSTITUTION = "bgscet"
PROFILE = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])
MUTT = "Mutt & Swamiji"
# BGSCET's footer links its own Instagram, the Math's pages and the Swamiji's X account, as the real site does.
HOME = """<html><head><title>BGS College of Engineering and Technology</title></head><body>
<main><p>BGS College of Engineering and Technology, Mahalakshmipuram, Bengaluru.</p></main>
<footer><a href="https://www.instagram.com/bgscet_engg_coll/">Instagram</a>
<a href="https://www.facebook.com/SriAdichunchanagiriMahasamsthanaMath/">Sri Adichunchanagiri Mahasamsthana Math</a>
<a href="https://www.instagram.com/sriadichunchanagirimahasamsthanamath/">Our Math</a>
<a href="https://x.com/Jaisrigurudev">Our Swamiji on X</a>
<a href="https://x.com/Thesjibt">News</a></footer></body></html>"""


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        sync_profile(self.store, PROFILE)
        self.service = MapService(self.store, seed_groups={INSTITUTION: ["BGSCET"]}, approvers={"mutt & swamiji": ["math-it-office"]})
        self.manager = principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION)
        self.approver = principal(PrincipalType.PRINCIPAL, "math-it-office", college_id=INSTITUTION)
        self.service.seed(
            self.manager, INSTITUTION, sweep_text=DEFAULT_SWEEP.read_text(encoding="utf-8"), lookalikes_text=DEFAULT_LOOKALIKES.read_text(encoding="utf-8"),
            groups=["BGSCET", MUTT], approved_by="Math IT office, 2026-09-30", holdout_percent=0,
        )

    def entity_of(self, key):
        return self.store.get_entity(INSTITUTION, self.store.find_asset(INSTITUTION, key)["entity_id"])

    def dispute(self, key):
        asset = self.store.find_asset(INSTITUTION, key)
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="dispute", title="two official X accounts", asset_id=asset["asset_id"], entity_id=asset["entity_id"], url=asset["url"])
        return review_id


class ImportTests(Base):
    def test_other_groups_entities_belong_to_their_group(self):
        swamiji = self.entity_of("x:jaisrigurudev")
        self.assertEqual((swamiji["authority"], self.store.get_entity(INSTITUTION, swamiji["parent_id"])["authority"]), (MUTT, MUTT))
        self.assertEqual(self.entity_of("instagram:bgscet_engg_coll")["authority"], "self", "the institution's own group stays its own")
        self.assertTrue(all(entity["authority"] == "external" for entity in self.store.list_entities(INSTITUTION, kind="lookalike")))
        # A later import never hands the Math back to the institution.
        self.store.upsert_entity(INSTITUTION, name=swamiji["name"], kind=swamiji["kind"])
        self.assertEqual(self.entity_of("x:jaisrigurudev")["authority"], MUTT)


class DecisionTests(Base):
    def test_a_bgscet_manager_cannot_settle_which_swamiji_account_is_real(self):
        real, other = self.dispute("x:jaisrigurudev"), self.dispute("x:srinns")
        with self.assertRaisesRegex(PermissionError, "Mutt & Swamiji"):
            self.service.decide(self.manager, INSTITUTION, real, decision="confirm", relation="official")
        with self.assertRaises(PermissionError):
            self.service.decide(self.manager, INSTITUTION, other, decision="impersonation")
        with self.assertRaises(PermissionError):
            self.service.decide(self.manager, INSTITUTION, other, decision="dismiss", relation="third_party")
        with self.assertRaises(PermissionError):
            self.service.suppress(self.manager, INSTITUTION, identifier="https://x.com/SriNNS", reason="not his")
        self.assertIsNotNone(self.store.find_asset(INSTITUTION, "x:srinns"))
        self.assertFalse(self.store.is_suppressed(INSTITUTION, "x:srinns"), "a refused suppression suppresses nothing")
        self.assertEqual(self.store.list_incidents(INSTITUTION), [], "no impersonation incident was raised")
        self.assertEqual({self.store.find_asset(INSTITUTION, key)["grade"] for key in ("x:jaisrigurudev", "x:srinns")}, {"unrated"})
        # Dismissing settles nothing, so any manager may.
        self.assertNotIn("authority", self.service.decide(self.manager, INSTITUTION, other, decision="dismiss"))

    def test_a_named_approver_decides_and_is_recorded(self):
        result = self.service.decide(self.approver, INSTITUTION, self.dispute("x:jaisrigurudev"), decision="confirm", relation="official")
        self.assertEqual((result["authority"], result["approver"], result["changes"][0]["to"]), (MUTT, "math-it-office", "B"))
        branch = self.store.find_asset(INSTITUTION, "web:acmbengaluru.org")
        removed = self.service.suppress(self.approver, INSTITUTION, identifier=branch["url"], reason="asked to be left out")
        self.assertEqual((removed["removed"], removed["authority"]), (True, MUTT), "a branch is under the Math's authority too")

    def test_the_institutions_own_entities_are_unaffected(self):
        result = self.service.decide(self.manager, INSTITUTION, self.dispute("instagram:bgscet_engg_coll"), decision="confirm", relation="official")
        self.assertEqual(result["changes"][0]["to"], "B")
        self.assertNotIn("authority", result)
        entity = sync_profile(self.store, PROFILE)["entity_id"]
        fake, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://www.instagram.com/bgscet.official/"), entity_id=entity)
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="calls itself official", asset_id=fake)
        self.service.decide(self.manager, INSTITUTION, review_id, decision="impersonation")
        self.assertEqual(self.store.get_asset(INSTITUTION, fake)["grade"], "D")


class HarvestTests(Base):
    def harvest(self):
        domain = self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")
        return asyncio.run(OfficialSiteHarvester(fetcher_for({"https://bgscet.ac.in/": (200, HOME)}), self.store).harvest(INSTITUTION, domain["asset_id"], run_id="r1"))

    def test_a_bgscet_footer_does_not_vouch_for_the_maths_accounts(self):
        result = self.harvest()
        own = self.store.find_asset(INSTITUTION, "instagram:bgscet_engg_coll")
        self.assertEqual((own["grade"], own["relation"]), ("A", "official"), "the site still vouches for its own accounts")
        bgscet = {entity["entity_id"] for entity in self.store.list_entities(INSTITUTION) if entity["authority"] == "self"}
        for key in ("x:jaisrigurudev", "facebook:sriadichunchanagirimahasamsthanamath", "instagram:sriadichunchanagirimahasamsthanamath"):
            asset = self.store.find_asset(INSTITUTION, key)
            self.assertIn(asset["grade"], {"C", "unrated"}, key)
            self.assertNotIn(asset["entity_id"], bgscet, f"{key} is not filed under BGSCET")
            kinds = {item["kind"] for item in self.store.list_evidence(INSTITUTION, asset_id=asset["asset_id"])}
            self.assertNotIn("official_link", kinds, key)
            self.assertIn("hub_link", kinds, key)
        self.assertEqual(self.entity_of("instagram:sriadichunchanagirimahasamsthanamath")["name"], "Sri Adichunchanagiri Mahasamsthana Math", "an unmapped account that names the Math is filed under it")
        # The footer does not pick between the Swamiji's two X accounts.
        regrade(self.store, INSTITUTION)
        self.assertEqual({self.store.find_asset(INSTITUTION, key)["grade"] for key in ("x:jaisrigurudev", "x:srinns")}, {"C"})
        self.assertIsNone(self.store.find_asset(INSTITUTION, "x:thesjibt")["entity_id"], "a look-alike's name holds a link back without filing it under the look-alike")
        self.assertEqual(set(result.held), {"x:jaisrigurudev", "facebook:sriadichunchanagirimahasamsthanamath", "instagram:sriadichunchanagirimahasamsthanamath", "x:thesjibt"})
        self.assertNotIn("x:jaisrigurudev", result.accounts)
        # Queued for the Math's approvers, and only they can confirm it.
        items = {item["url"]: item for item in self.store.list_review_items(INSTITUTION, kind="candidate_account")}
        swamiji = items["https://x.com/jaisrigurudev"]
        with self.assertRaises(PermissionError):
            self.service.decide(self.manager, INSTITUTION, swamiji["review_id"], decision="confirm", relation="official")
        self.assertEqual(self.service.decide(self.approver, INSTITUTION, swamiji["review_id"], decision="confirm", relation="official")["changes"][0]["to"], "B")
        # Harvesting again queues nothing twice.
        self.harvest()
        self.assertEqual(len(self.store.list_review_items(INSTITUTION, kind="candidate_account", status=None)), len(items))

    def test_an_official_link_recorded_before_is_withdrawn(self):
        domain = self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")
        swamiji = self.store.find_asset(INSTITUTION, "x:jaisrigurudev")
        self.store.add_evidence(INSTITUTION, asset_id=swamiji["asset_id"], kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=domain["asset_id"], channel="site:bgscet.ac.in")
        regrade(self.store, INSTITUTION, [swamiji["asset_id"]])
        self.assertEqual(self.store.find_asset(INSTITUTION, "x:jaisrigurudev")["grade"], "A", "the old harvester's verdict")
        self.harvest()
        self.assertEqual(self.store.find_asset(INSTITUTION, "x:jaisrigurudev")["grade"], "C")


class SettingsTests(unittest.TestCase):
    def test_approvers_and_contacts_are_validated(self):
        settings = AppSettings(intelligence_entity_approvers='{"Mutt & Swamiji": ["math-it-office"]}', intelligence_authority_contacts='{"Mutt & Swamiji": ["it@adichunchanagiri.org"]}')
        self.assertEqual(settings.intelligence_entity_approver_map(), {MUTT: ("math-it-office",)})
        self.assertEqual(settings.intelligence_authority_contact_map(), {MUTT: ("it@adichunchanagiri.org",)})
        for broken in (AppSettings(intelligence_entity_approvers="not json"), AppSettings(intelligence_entity_approvers='{"Mutt & Swamiji": "math-it-office"}'), AppSettings(intelligence_authority_contacts='{"Mutt & Swamiji": ["the math"]}')):
            with self.assertRaises(ValueError):
                broken.ensure_safe_for_production()


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class AuthorityRouteTests(unittest.TestCase):
    def test_a_decision_on_the_maths_entity_is_403_unless_approved_and_the_approver_is_audited(self):
        client = TestClient(app)
        college = "auth_college"
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "auth-principal", "X-Demo-Role": "principal", "X-Demo-College": college}
        approver = {**headers, "X-Demo-Principal": "auth-math-it"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        # The Swamiji's entity is the institution's own by default; the Math above it is not.
        math = store.upsert_entity(college, name="Sri Adichunchanagiri Mahasamsthana Math", kind="organisation", group_label=MUTT, authority=MUTT)
        swamiji = store.upsert_entity(college, name="Sri Sri Sri Dr. Nirmalanandanatha Mahaswamiji", kind="role", parent_id=math)
        asset_id, _ = store.upsert_asset(college, asset_ref("https://x.com/SriNNS"), entity_id=swamiji, relation="official")
        review_id, _ = store.add_review_item(college, kind="dispute", title="two official X accounts", asset_id=asset_id, entity_id=swamiji)
        path = f"/v1/intelligence/map/review/{review_id}/decision"
        with mock.patch.object(platform, "intelligence_map", MapService(store, approvers={MUTT: ["auth-math-it"]})):
            refused = client.post(path, headers=headers, json={"decision": "impersonation"})
            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertIn("Mutt & Swamiji", refused.text)
            decided = client.post(path, headers=approver, json={"decision": "reject", "note": "not his"})
            self.assertEqual(decided.status_code, 200, decided.text)
        events = [event for event in app.state.runtime.store.recent_audit(limit=50) if event.event_type == "intelligence.map.review_decision" and dict(event.decision_metadata).get("review_id") == review_id]
        outcomes = {event.outcome: dict(event.decision_metadata) for event in events}
        self.assertIn(AuditOutcome.DENIED, outcomes)
        self.assertEqual((outcomes[AuditOutcome.SUCCESS]["authority"], outcomes[AuditOutcome.SUCCESS]["approver"]), (MUTT, "auth-math-it"))


if __name__ == "__main__":
    unittest.main()
