import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorRegistry, ConnectorResult
from app.internet_intelligence.map.engine import EngineConfig, MapEngine
from app.internet_intelligence.map.gate import rescore
from app.internet_intelligence.map.grading import SCORER_VERSION
from app.internet_intelligence.map.incidents import IncidentDesk
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from platform_fixtures import principal

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
INSTITUTION = "bgscet"
PROFILE = InstitutionProfile(INSTITUTION, "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])


class Step:
    """A connector whose run does whatever the test says (adds evidence, reports incidents)."""

    def __init__(self, name, action, *, budget_key="fetch", cost=0):
        self.name, self.access_mode, self.budget_key, self.max_grade, self.default_interval = name, "public_page", budget_key, "C", 86400
        self.action, self._cost, self.runs = action, cost, 0

    def enabled(self):
        return True

    def cost(self, source):
        return self._cost

    async def run(self, source, context):
        self.runs += 1
        return self.action(source, context) or ConnectorResult(outcome="ok")


class Base(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.entity_id = sync_profile(self.store, PROFILE)["entity_id"]
        regrade(self.store, INSTITUTION)
        self.service = MapService(self.store)
        self.manager = principal(PrincipalType.PRINCIPAL, college_id=INSTITUTION)
        self.reader = principal(PrincipalType.FACULTY, college_id=INSTITUTION)

    def asset(self, url, *, relation="official", entity=True):
        asset_id, _ = self.store.upsert_asset(INSTITUTION, asset_ref(url), entity_id=self.entity_id if entity else None, relation=relation)
        return asset_id

    def evidence(self, asset_id, *channels):
        for channel in channels:
            kind = "community_record" if channel == "wikidata" else "search_snippet"
            self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind=kind, channel=channel, observed_via="index")

    def tick(self, *connectors, engine=None, **config):
        engine = engine or MapEngine(self.store, ConnectorRegistry(list(connectors)), EngineConfig(**config), clock=lambda: NOW)
        return asyncio.run(engine.tick(INSTITUTION))

    def grade(self, asset_id):
        return self.store.get_asset(INSTITUTION, asset_id)["grade"]


class HeldPassTests(Base):
    def setUp(self):
        super().setUp()
        self.store.add_gold(INSTITUTION, asset_key="web:bgsgroupofinstitutions.com", platform="website", split="canary", entity_name="BGS Group of Institutions", relation="third_party", expected_min_grade="D")
        self.canary = self.asset("https://www.bgsgroupofinstitutions.com/", relation="unknown", entity=False)
        self.sibling = self.asset("https://www.instagram.com/bgs_group_official/")

        def misled(source, context):
            # The same two misleading channels vouch for the canary and for its sibling.
            for asset_id in (self.canary, self.sibling):
                self.evidence(asset_id, "search:tavily", "wikidata")
            return ConnectorResult(outcome="ok", touched={self.canary, self.sibling}, yield_count=2)

        self.connector = Step("misled", misled)
        self.store.upsert_source(INSTITUTION, connector="misled", target="t", due_at=NOW.isoformat())

    def test_what_a_leaking_pass_raised_stays_unpublished_until_a_manager_decides(self):
        result = self.tick(self.connector)
        self.assertEqual(result["gate"], "held")
        self.assertEqual(self.grade(self.canary), "D", "the canary correction itself stands")
        sibling = self.store.get_asset(INSTITUTION, self.sibling)
        self.assertEqual((sibling["grade"], sibling["proposed_grade"], sibling["proposed_run_id"]), ("unrated", "B", result["run_id"]), "what the same channels raised waits")
        self.assertNotIn(self.sibling, [asset["asset_id"] for asset in self.service.assets(self.reader, INSTITUTION)], "readers never see a held result")
        self.assertTrue(all(not key.startswith("proposed_") for asset in self.service.assets(self.reader, INSTITUTION) for key in asset))
        digest = IncidentDesk(self.store).digest(INSTITUTION, now=NOW + timedelta(hours=1))
        self.assertEqual((digest["found"], digest["raised"], digest["held"]), (0, 0, 1), "a held pass's findings are not reported as found")
        [gate_item] = self.store.list_review_items(INSTITUTION, kind="run_gate")
        self.assertIn("known look-alike", gate_item["detail"])

        with self.subTest("a later pass does not publish around the gate"):
            self.store.upsert_source(INSTITUTION, connector="misled", target="t2", due_at=NOW.isoformat())
            later = self.tick(self.connector)
            self.assertEqual(later["gate"], "passed")
            self.assertEqual(self.grade(self.sibling), "unrated")
            self.assertEqual(self.store.get_asset(INSTITUTION, self.sibling)["proposed_run_id"], result["run_id"], "still the held pass's proposal")

        decided = self.service.decide(self.manager, INSTITUTION, gate_item["review_id"], decision="publish")
        self.assertEqual(decided["published"], 1)
        self.assertEqual(self.grade(self.sibling), "B")
        self.assertEqual(self.grade(self.canary), "D")

    def test_discarding_keeps_readers_where_they_were(self):
        result = self.tick(self.connector)
        [gate_item] = self.store.list_review_items(INSTITUTION, kind="run_gate")
        self.assertEqual(self.service.decide(self.manager, INSTITUTION, gate_item["review_id"], decision="discard")["discarded"], 1)
        sibling = self.store.get_asset(INSTITUTION, self.sibling)
        self.assertEqual((sibling["grade"], sibling["proposed_grade"]), ("unrated", None))
        self.assertEqual(result["counts"]["held"], 1)


class RegressionTests(Base):
    def test_a_pass_that_loses_ground_truth_is_held_but_security_downgrades_stand(self):
        seed = self.asset("https://www.instagram.com/bgscet_engg_coll/")
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgscet_engg_coll", platform="instagram", split="holdout", expected_min_grade="B")
        self.evidence(seed, "search:tavily", "wikidata")
        hacked = self.asset("https://www.facebook.com/bgscet.official/")
        self.evidence(hacked, "search:tavily", "wikidata")
        regrade(self.store, INSTITUTION)
        self.assertEqual((self.grade(seed), self.grade(hacked)), ("B", "B"))

        def breaks(source, context):
            for asset_id in (seed, hacked):
                self.store.add_evidence(INSTITUTION, asset_id=asset_id, kind="reviewer_reject", polarity="refutes", channel="bad-run", observed_via="index")
            self.store.add_evidence(INSTITUTION, asset_id=hacked, kind="integrity", polarity="refutes", detail="compromised:spam_links", channel="fetch", observed_via="live")
            return ConnectorResult(outcome="ok", touched={seed, hacked})

        self.store.upsert_source(INSTITUTION, connector="breaks", target="t", due_at=NOW.isoformat())
        result = self.tick(Step("breaks", breaks))
        self.assertEqual(result["gate"], "held")
        self.assertEqual(self.grade(seed), "B", "the holdout item readers saw is not taken away by a pass that lost ground truth")
        self.assertEqual(self.store.get_asset(INSTITUTION, seed)["proposed_grade"], "D")
        self.assertEqual(self.store.get_asset(INSTITUTION, hacked)["status"], "compromised")
        self.assertEqual(self.grade(hacked), "D", "a site out of the institution's hands is never shown as fine again")

    def test_a_proposal_never_rewrites_the_published_reasons_and_runs_are_decided_apart(self):
        asset_id = self.asset("https://www.instagram.com/bgscet_engg_coll/")
        self.evidence(asset_id, "search:tavily", "wikidata")
        regrade(self.store, INSTITUTION)
        published = self.store.get_asset(INSTITUTION, asset_id)["grade_reasons"]
        other = self.asset("https://x.com/bgscet_official")
        self.evidence(other, "search:tavily")
        regrade(self.store, INSTITUTION)
        self.store.set_grade(INSTITUTION, asset_id, grade="C", reasons=["proposed by run one"], scorer_version=SCORER_VERSION, proposed=True, run_id="run-one")
        self.store.set_grade(INSTITUTION, other, grade="B", reasons=["proposed by run two"], scorer_version=SCORER_VERSION, proposed=True, run_id="run-two")
        self.assertEqual(self.store.get_asset(INSTITUTION, asset_id)["grade_reasons"], published)
        self.assertEqual(self.store.apply_proposed(INSTITUTION, run_id="run-one"), 1)
        self.assertEqual((self.grade(asset_id), self.store.get_asset(INSTITUTION, asset_id)["grade_reasons"]), ("C", ["proposed by run one"]))
        self.assertEqual((self.grade(other), self.store.get_asset(INSTITUTION, other)["proposed_grade"]), ("C", "B"), "run two's proposal is untouched")
        self.assertEqual(self.store.discard_proposed(INSTITUTION, run_id="run-two"), 1)

    def test_a_new_scoring_rule_reaches_readers_only_through_the_gate(self):
        asset_id = self.asset("https://www.instagram.com/bgscet_engg_coll/")
        self.evidence(asset_id, "search:tavily", "wikidata")
        regrade(self.store, INSTITUTION)
        self.store.set_grade(INSTITUTION, asset_id, grade="C", reasons=["old rule"], scorer_version="grader-0")
        self.assertTrue(self.store.has_stale_scores(INSTITUTION, SCORER_VERSION))
        regrade(self.store, INSTITUTION, [asset_id])
        self.assertEqual((self.grade(asset_id), self.store.get_asset(INSTITUTION, asset_id)["proposed_grade"]), ("C", "B"), "an ordinary regrade parks a rule change")
        self.tick()
        self.assertEqual(self.grade(asset_id), "B", "the pass ran the gated re-scoring, which passed")
        self.assertFalse(self.store.has_stale_scores(INSTITUTION, SCORER_VERSION))

    def test_a_held_rescoring_names_its_own_proposal(self):
        self.store.add_gold(INSTITUTION, asset_key="instagram:bgscet_engg_coll", platform="instagram", split="holdout", expected_min_grade="B")
        asset_id = self.asset("https://www.instagram.com/bgscet_engg_coll/")
        self.store.set_grade(INSTITUTION, asset_id, grade="B", reasons=["old rule"], scorer_version="grader-0")
        verdict = rescore(self.store, INSTITUTION, run_id="rescore:test")
        self.assertFalse(verdict.passed)
        self.assertEqual(self.store.get_asset(INSTITUTION, asset_id)["proposed_run_id"], "rescore:test")
        [item] = self.store.list_review_items(INSTITUTION, kind="run_gate")
        self.assertEqual(item["run_id"], "rescore:test")


class EngineOrderTests(Base):
    def test_incidents_are_kept_when_the_pass_fails_after_its_sources_ran(self):
        def finds(source, context):
            return ConnectorResult(outcome="ok", incidents=[{"kind": "site_compromised", "target": "bgscet.ac.in", "signals": ["spam links"]}])

        self.store.upsert_source(INSTITUTION, connector="finds", target="t", due_at=NOW.isoformat())
        engine = MapEngine(self.store, ConnectorRegistry([Step("finds", finds)]), clock=lambda: NOW, incidents=IncidentDesk(self.store))
        with mock.patch("app.internet_intelligence.map.engine.gate_pass", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual([row["kind"] for row in self.store.list_incidents(INSTITUTION)], ["site_compromised"])
        self.assertEqual(self.store.list_map_runs(INSTITUTION, kind="tick")[0]["status"], "failed")

    def test_free_work_is_claimed_before_paid_work(self):
        feed, search = Step("feedish", lambda s, c: None, budget_key="feed"), Step("searchish", lambda s, c: None, budget_key="search", cost=1)
        for name in ("feedish", "searchish"):
            self.store.upsert_source(INSTITUTION, connector=name, target=f"{name}-1", work_class="rotation", due_at=NOW.isoformat())
        result = self.tick(feed, search, sources_per_tick=1)
        self.assertEqual((feed.runs, search.runs), (1, 0), "the RSS feed runs, the paid search waits")
        self.assertEqual(result["spend"], {"feed": 0.0})

    def test_refunds_hand_back_what_was_not_spent(self):
        day = NOW.date().isoformat()
        self.assertTrue(self.store.reserve_budget(INSTITUTION, connector="fetch", units=5, day=day, global_cap=100, tenant_cap=50))
        self.store.refund_budget(INSTITUTION, connector="fetch", units=4, day=day)
        self.assertEqual((self.store.tenant_spend(INSTITUTION, day=day)["fetch"]["units"], self.store.spend(day=day)["fetch"]["units"]), (1.0, 1.0))
        self.store.refund_budget(INSTITUTION, connector="fetch", units=9, day=day)
        self.assertEqual(self.store.tenant_spend(INSTITUTION, day=day)["fetch"]["units"], 0.0, "never below zero")


class DiscoveryTests(Base):
    def test_discovery_pauses_after_two_dry_passes_and_resumes_on_something_new(self):
        explorer = Step("explorer", lambda s, c: None)
        clock = {"now": NOW}
        engine = MapEngine(self.store, ConnectorRegistry([explorer]), EngineConfig(min_interval=60), clock=lambda: clock["now"])

        def due_lead(index):
            self.store.upsert_source(INSTITUTION, connector="explorer", target=f"https://lead{index}.example/", origin="lead", work_class="explore", due_at=clock["now"].isoformat())

        for index in range(2):
            due_lead(index)
            asyncio.run(engine.tick(INSTITUTION))
            clock["now"] += timedelta(hours=1)
        self.assertEqual(explorer.runs, 2)
        due_lead(2)
        paused = asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual((explorer.runs, paused["counts"]["discovery_paused"], paused["stop_reason"]), (2, 1, "discovery_paused"), "two dry passes in a row: exploration stops")

        # Something new arrives from a watch: discovery resumes.
        clock["now"] += timedelta(hours=1)
        watch = Step("watch", lambda s, c: ConnectorResult(outcome="ok", yield_count=1, new_assets=["web:new.example"]))
        engine.registry.register(watch)
        self.store.upsert_source(INSTITUTION, connector="watch", target="w", origin="recurring", work_class="rotation", due_at=clock["now"].isoformat())
        asyncio.run(engine.tick(INSTITUTION))
        clock["now"] += timedelta(hours=1)
        asyncio.run(engine.tick(INSTITUTION))
        self.assertEqual(explorer.runs, 3, "the waiting lead ran once discovery resumed")

    def test_a_lead_that_pays_off_becomes_a_regular_watch(self):
        finder = Step("finder", lambda s, c: ConnectorResult(outcome="ok", yield_count=1, new_assets=["web:x.example"]))
        source_id, _ = self.store.upsert_source(INSTITUTION, connector="finder", target="https://lead.example/", origin="lead", work_class="explore", due_at=NOW.isoformat(), expires_at=(NOW + timedelta(days=30)).isoformat())
        self.tick(finder)
        source = self.store.get_source(INSTITUTION, source_id)
        self.assertEqual((source["origin"], source["work_class"], source["expires_at"]), ("recurring", "rotation", None), "pausing discovery never pauses it")


class ManualHarvestTests(Base):
    def test_a_manual_harvest_records_what_it_finds_at_once(self):
        from test_intelligence_map_engine import HOME, Site

        hacked = HOME.replace("<footer>", "<footer>" + "".join(f'<a href="https://casino{index}.example/" style="display:none">slots</a>' for index in range(4)))
        desk = IncidentDesk(self.store)
        service = MapService(self.store, fetcher=Site({"https://bgscet.ac.in/": (200, hacked)}).fetcher(), desk=desk)
        result = asyncio.run(service.harvest(self.manager, INSTITUTION))
        self.assertIn("site_compromised", [incident["kind"] for item in result["results"] for incident in item["incidents"]])
        self.assertEqual([row["kind"] for row in self.store.list_incidents(INSTITUTION)], ["site_compromised"], "recorded now, not at the next weekly pass")
        self.assertIn("gate", result)



class ProfileRemovalTests(Base):
    def test_a_domain_taken_out_of_the_profile_stops_anchoring_until_named_again(self):
        from app.internet_intelligence.map.ownership import nominated_by_institution
        from app.internet_intelligence.map.pipeline import nominated

        domain = self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")
        self.assertEqual(domain["grade"], "A")
        moved = InstitutionProfile(INSTITUTION, PROFILE.name, "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.edu.in"])
        result = sync_profile(self.store, moved)
        self.assertEqual(result["domains_removed"], [domain["asset_id"]])
        self.assertNotEqual(self.grade(domain["asset_id"]), "A", "no longer the institution's say-so")
        self.assertFalse(nominated(self.store, INSTITUTION, domain["asset_id"]), "no longer watched as the institution's site")
        self.assertFalse(nominated_by_institution(self.store, INSTITUTION, domain["asset_id"]), "and no longer owner-checked")
        self.assertEqual(sync_profile(self.store, moved)["domains_removed"], [], "withdrawn once")
        sync_profile(self.store, PROFILE)
        regrade(self.store, INSTITUTION, [domain["asset_id"]])
        self.assertEqual(self.grade(domain["asset_id"]), "A", "named again, it anchors again")
        self.assertTrue(nominated(self.store, INSTITUTION, domain["asset_id"]))


class ReviewerNoteTests(Base):
    def test_a_reviewers_note_loses_peoples_names_before_it_travels_in_an_alert(self):
        account = self.asset("https://www.instagram.com/bgscet.officiall/", relation="unknown")
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="calls itself official", asset_id=account, url="https://www.instagram.com/bgscet.officiall/")
        effect = self.service.decide(self.manager, INSTITUTION, review_id, decision="impersonation", note="Reported by Ravi Kumar, student of BGS College of Engineering and Technology")
        [signal] = effect["incident"]["signals"]
        self.assertNotIn("Ravi Kumar", signal)
        self.assertIn("BGS College of Engineering and Technology", signal, "the institution's own name stays")


class ProvenanceTests(Base):
    def test_a_hub_found_by_the_same_search_is_not_a_second_channel(self):
        hub = self.asset("https://linktr.ee/bgscet")
        account = self.asset("https://www.instagram.com/bgscet/")
        self.evidence(hub, "search:stub")
        self.evidence(account, "search:stub")
        self.store.add_evidence(INSTITUTION, asset_id=account, kind="hub_link", detail="C:hub", source_asset_id=hub, channel="hub:bgscet", observed_via="live")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade(account), "C", "the search found the hub too: that is one channel, not two")
        # Once the hub stands on something else (a directory listing), it is a second channel.
        self.store.add_evidence(INSTITUTION, asset_id=hub, kind="directory_record", detail="listing:unstop.com", channel="directory:unstop", observed_via="index")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade(account), "B")


class PendingProposalTests(Base):
    def setUp(self):
        super().setUp()
        self.account_id = self.asset("https://www.instagram.com/bgscet_official_fake/")
        self.evidence(self.account_id, "search:tavily", "wikidata")
        regrade(self.store, INSTITUTION)
        self.assertEqual(self.grade(self.account_id), "B")
        # An unrelated held pass left a proposal waiting on it.
        self.store.set_grade(INSTITUTION, self.account_id, grade="A", reasons=["held"], scorer_version=SCORER_VERSION, proposed=True, run_id="held-run")

    def test_a_reviewers_verdict_is_not_swallowed_by_a_waiting_proposal(self):
        review_id, _ = self.store.add_review_item(INSTITUTION, kind="impersonation_candidate", title="copies our logo", asset_id=self.account_id, url="https://www.instagram.com/bgscet_official_fake/")
        self.service.decide(self.manager, INSTITUTION, review_id, decision="impersonation", note="copies our logo")
        self.assertEqual((self.grade(self.account_id), self.store.get_asset(INSTITUTION, self.account_id)["proposed_grade"]), ("D", None))
        self.assertNotIn(self.account_id, [asset["asset_id"] for asset in self.service.assets(self.reader, INSTITUTION)])

    def test_a_security_downgrade_is_not_swallowed_either(self):
        domain = self.store.find_asset(INSTITUTION, "web:bgscet.ac.in")["asset_id"]
        self.store.set_grade(INSTITUTION, domain, grade="A", reasons=["held"], scorer_version=SCORER_VERSION, proposed=True, run_id="held-run")
        self.store.add_evidence(INSTITUTION, asset_id=domain, kind="integrity", polarity="refutes", detail="hijacked:gambling", channel="fetch", observed_via="live")
        regrade(self.store, INSTITUTION, [domain])
        self.assertEqual((self.grade(domain), self.store.get_asset(INSTITUTION, domain)["status"]), ("D", "hijacked"))


class ProfileAuthorityTests(Base):
    def test_the_profile_cannot_make_another_groups_domain_the_institutions_own(self):
        math = self.store.upsert_entity(INSTITUTION, name="Sri Adichunchanagiri Mahasamsthana Math", kind="organisation", group_label="Mutt & Swamiji", authority="Mutt & Swamiji")
        domain, _ = self.store.upsert_asset(INSTITUTION, asset_ref("https://acmbengaluru.org/"), entity_id=math, relation="official")
        self.store.add_evidence(INSTITUTION, asset_id=domain, kind="imported_claim", channel="seed", observed_via="import")
        regrade(self.store, INSTITUTION, [domain])
        widened = InstitutionProfile(INSTITUTION, PROFILE.name, "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in", "acmbengaluru.org"])
        result = sync_profile(self.store, widened)
        self.assertEqual(result["domains_refused"], ["acmbengaluru.org"])
        regrade(self.store, INSTITUTION, [domain])
        self.assertEqual(self.grade(domain), "C", "only the Math's approvers may settle the Math's domain")


if __name__ == "__main__":
    unittest.main()
