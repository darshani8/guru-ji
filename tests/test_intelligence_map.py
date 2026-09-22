import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.metrics import map_metrics, record_baseline
from app.internet_intelligence.map.seed import (
    DEFAULT_SWEEP,
    import_seed,
    in_holdout,
    load_default_seed,
    parse_lookalikes,
    parse_sweep,
)
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore, render_sql_migration, suppression_fingerprint
from platform_fixtures import principal

REPO_ROOT = Path(__file__).resolve().parents[1]
FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    # Imported at collection time like the other route tests: a later module
    # reloads app.main with other settings, and this must keep the original.
    from fastapi.testclient import TestClient

    from app.main import app

SWEEP_HEADER = "group\tsubject\tsubject_kind\tparent\tlabel\turl\tclaimed_grade\trelation\tnote\n"


def sweep(*rows: str) -> str:
    return SWEEP_HEADER + "".join(row + "\n" for row in rows)


class AssetKeyTests(unittest.TestCase):
    def test_one_account_one_key_whatever_the_url_form(self):
        same = ["https://www.instagram.com/BGSCET_Engg_Coll/?igsh=MXN1", "instagram.com/bgscet_engg_coll", "https://m.instagram.com/bgscet_engg_coll/reels"]
        self.assertEqual({asset_ref(url).key for url in same}, {"instagram:bgscet_engg_coll"})
        self.assertEqual(asset_ref("https://mobile.twitter.com/SJBITOfficial").key, asset_ref("https://x.com/sjbitofficial").key)
        self.assertEqual(asset_ref("https://in.linkedin.com/company/SJC-Institute-of-Technology").key, "linkedin:company:sjc-institute-of-technology")

    def test_facebook_page_ids_are_found_in_every_url_form(self):
        for url in (
            "https://www.facebook.com/profile.php?id=100093242520553",
            "https://www.facebook.com/p/BGSCET-100093242520553/",
            "https://www.facebook.com/people/Some-Name/100093242520553/",
            "https://www.facebook.com/pages/Some%20Name/100093242520553",
            "https://www.facebook.com/Some-Name-100093242520553",
            "https://www.facebook.com/100093242520553/photos/",
        ):
            self.assertEqual(asset_ref(url).key, "facebook:id:100093242520553", url)
        self.assertEqual(asset_ref("https://www.facebook.com/bgscet2022/").key, "facebook:bgscet2022", "a short year is not a page ID")
        self.assertEqual(asset_ref("https://www.facebook.com/groups/612933268742004").kind, "group")
        self.assertEqual(asset_ref("https://www.facebook.com/sharer/sharer.php?u=x").kind, "page")

    def test_youtube_channel_ids_keep_their_case_and_videos_are_pages(self):
        self.assertEqual(asset_ref("https://www.youtube.com/channel/UClACJA8pf4QbkdH4__7MTGA").key, "youtube:channel:UClACJA8pf4QbkdH4__7MTGA")
        self.assertEqual(asset_ref("https://www.youtube.com/@BGSCET_OFFICIAL").key, "youtube:@bgscet_official")
        self.assertEqual(asset_ref("https://youtu.be/cMLBmGc4DG8").key, "youtube:video:cMLBmGc4DG8")

    def test_sites_pages_groups_and_posts(self):
        self.assertEqual(asset_ref("https://www.bgscet.ac.in/").key, "web:bgscet.ac.in")
        self.assertEqual(asset_ref("https://bgscet.ac.in").kind, "domain")
        self.assertEqual(asset_ref("http://acmbengaluru.org/hostels.php").kind, "page")
        self.assertEqual(asset_ref("https://www.reddit.com/r/SJBIT/").key, "reddit:r:sjbit")
        self.assertEqual(asset_ref("https://www.reddit.com/r/kcet/comments/1vlnnlq/title/").key, "reddit:post:1vlnnlq")
        self.assertEqual(asset_ref("https://chat.whatsapp.com/B8VAkhaf35gH1Fxy53jApc").kind, "group")
        self.assertEqual(asset_ref("https://x.com/narendramodi/status/2044362710356525529").kind, "page")
        self.assertEqual(asset_ref("https://www.instagram.com/popular/adichunchanagiri-temple/").kind, "page")
        with self.assertRaises(ValueError):
            asset_ref("mailto:someone@example.com")


class MapStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def test_entities_merge_names_and_assets_are_idempotent(self):
        first = self.store.upsert_entity("bgscet", name="BGS College of Engineering & Technology (BGSCET)", names=["BGSCET"])
        again = self.store.upsert_entity("bgscet", name="BGS College of Engineering & Technology (BGSCET)", names=["ಬಿಜಿಎಸ್ ಸಿಇಟಿ"])
        self.assertEqual(first, again)
        self.assertEqual(self.store.get_entity("bgscet", first)["names"], ["BGSCET", "ಬಿಜಿಎಸ್ ಸಿಇಟಿ"])
        with self.assertRaises(ValueError):
            self.store.upsert_entity("bgscet", name="x", kind="planet")
        ref = asset_ref("https://www.instagram.com/bgscet_engg_coll/")
        asset_id, created = self.store.upsert_asset("bgscet", ref, entity_id=first)
        self.assertTrue(created)
        same, created = self.store.upsert_asset("bgscet", asset_ref("instagram.com/BGSCET_ENGG_COLL"), entity_id=None, relation="official")
        self.assertEqual((same, created), (asset_id, False))
        stored = self.store.get_asset("bgscet", asset_id)
        self.assertEqual((stored["relation"], stored["entity_id"], stored["grade"]), ("official", first, "unrated"), "an unknown relation is filled in; the link stays")
        self.store.upsert_asset("bgscet", ref, entity_id=None, relation="community")
        self.assertEqual(self.store.get_asset("bgscet", asset_id)["relation"], "official", "a known relation is never overwritten by a later claim")
        self.assertIsNone(self.store.find_asset("other", ref.key), "maps are per institution")

    def test_evidence_is_append_only_and_validated(self):
        asset_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://bgscet.ac.in/"), entity_id=None)
        self.store.add_evidence("bgscet", asset_id=asset_id, kind="imported_claim", observed_via="import")
        self.store.add_evidence("bgscet", asset_id=asset_id, kind="liveness", polarity="refutes", observed_via="live")
        self.assertEqual([item["kind"] for item in self.store.list_evidence("bgscet", asset_id=asset_id)], ["imported_claim", "liveness"])
        with self.assertRaises(ValueError):
            self.store.add_evidence("bgscet", asset_id=asset_id, kind="x", polarity="maybe")
        with self.assertRaises(ValueError):
            self.store.add_evidence("bgscet", asset_id=asset_id, kind="x", observed_via="rumour")
        self.assertFalse(any(name.startswith(("delete", "update", "remove")) and "evidence" in name for name in dir(self.store)), "no way to rewrite the log")

    def test_suppression_stores_only_a_keyed_fingerprint(self):
        fingerprint = self.store.suppress("bgscet", "instagram:some.student", reason="personal account")
        self.assertTrue(self.store.is_suppressed("bgscet", "Instagram:Some.Student "))
        self.assertFalse(self.store.is_suppressed("other", "instagram:some.student"))
        self.assertNotEqual(fingerprint, suppression_fingerprint(b"another-key", "instagram:some.student"), "the fingerprint depends on the secret key")
        rows = self.store.backend.fetchall("SELECT * FROM intel_suppression")
        self.assertNotIn("some.student", str(rows))

    def test_checked_in_migration_matches_generator(self):
        checked_in = (REPO_ROOT / "migrations" / "004_intelligence_map.sql").read_text(encoding="utf-8")
        self.assertEqual(checked_in, render_sql_migration(), "regenerate migrations/004_intelligence_map.sql with render_sql_migration()")

    def test_intelligence_migration_mirror_matches(self):
        from app.internet_intelligence.store import render_sql_migration as intelligence_sql

        self.assertEqual((REPO_ROOT / "migrations" / "003_internet_intelligence.sql").read_text(encoding="utf-8"), intelligence_sql())


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def test_the_bundled_sweep_parses(self):
        rows, lookalikes = load_default_seed()
        self.assertGreater(len(rows), 400)
        self.assertTrue(any(row.url == "https://www.instagram.com/bgscet_engg_coll/" and row.claimed_grade == "A" for row in rows))
        self.assertTrue(all(row.relation in {"official", "unknown", "community", "third_party"} for row in rows))
        self.assertTrue(any(item.name == "British Geological Survey" for item in lookalikes))
        self.assertTrue(any(item.url == "https://acmbgs.org/" for item in lookalikes), "the parked former domain is a canary")

    def test_malformed_sweeps_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_sweep("group\turl\nX\thttps://x.example\n")
        with self.assertRaises(ValueError):
            parse_sweep(sweep("G\tS\tinstitution\t\tL\thttps://x.example/\tZ\tofficial\tn"))
        with self.assertRaises(ValueError):
            parse_sweep(sweep("G\tS\tinstitution\t\tL\thttps://x.example/\tA\tbest-friend\tn"))

    def test_import_holds_out_ground_truth_and_seeds_the_rest(self):
        rows, lookalikes = load_default_seed()
        summary = import_seed(self.store, "bgscet", rows, lookalikes=lookalikes, holdout_percent=20)
        self.assertGreater(summary.holdout, 50)
        self.assertLess(summary.holdout, 130)
        holdout = self.store.holdout_keys("bgscet")
        seeded = {asset["asset_key"] for asset in self.store.list_assets("bgscet", limit=5000)}
        self.assertFalse(holdout & seeded, "hidden ground truth is never seeded")
        self.assertEqual(len(seeded), summary.assets_created)
        self.assertTrue(all(asset["grade"] == "unrated" for asset in self.store.list_assets("bgscet", limit=5000)), "claims are not grades")
        canaries = self.store.list_gold("bgscet", split="canary")
        self.assertIn("web:acmbgs.org", {item["asset_key"] for item in canaries})
        self.assertFalse({item["asset_key"] for item in canaries} & seeded, "canaries are traps, not seeds")
        self.assertTrue(self.store.list_entities("bgscet", kind="lookalike"))
        evidence = self.store.list_evidence("bgscet", limit=5000)
        self.assertEqual({item["kind"] for item in evidence}, {"imported_claim"})
        # Re-importing is idempotent and keeps the same split.
        again = import_seed(self.store, "bgscet", rows, lookalikes=lookalikes, holdout_percent=20)
        self.assertEqual(again.assets_created, 0)
        self.assertEqual(self.store.holdout_keys("bgscet"), holdout)

    def test_every_lookalike_url_is_a_canary_even_when_the_sweep_lists_it(self):
        rows, lookalikes = load_default_seed()
        with_url = {asset_ref(item.url).key for item in lookalikes if item.url}
        listed = {asset_ref(row.url).key for row in rows} & with_url
        self.assertTrue(listed, "the bundled sweep does list some look-alike URLs (the collision this guards)")
        summary = import_seed(self.store, "bgscet", rows, lookalikes=lookalikes, holdout_percent=20)
        self.assertEqual({item["asset_key"] for item in self.store.list_gold("bgscet", split="canary")}, with_url)
        self.assertEqual(map_metrics(self.store, "bgscet")["canary_total"], len(with_url))
        self.assertEqual(set(summary.canary_collisions), listed)
        for key in listed:
            self.assertIsNone(self.store.find_asset("bgscet", key), "a look-alike is never seeded as an ordinary asset")

    def test_a_lookalike_seeded_before_is_turned_into_a_canary(self):
        row = "Group\tBGS Group\tinstitution\t\t\thttps://www.bgsgroupofinstitutions.com/\tF\tthird_party\t"
        import_seed(self.store, "bgscet", parse_sweep(sweep(row)), holdout_percent=0)
        asset = self.store.find_asset("bgscet", "web:bgsgroupofinstitutions.com")
        self.assertIsNotNone(asset)
        lookalike = parse_lookalikes("name\tnames\tlocations\turl\twhy\nBGS Group of Institutions\t\t\thttps://www.bgsgroupofinstitutions.com/\tunrelated lookalike\n")
        import_seed(self.store, "bgscet", [], lookalikes=lookalike)
        self.assertEqual([item["split"] for item in self.store.list_gold("bgscet") if item["asset_key"] == "web:bgsgroupofinstitutions.com"], ["canary"])
        from app.internet_intelligence.map.pipeline import regrade

        regrade(self.store, "bgscet", [asset["asset_id"]])
        self.assertEqual(self.store.get_asset("bgscet", asset["asset_id"])["grade"], "D")

    def test_grades_see_the_newest_evidence_however_long_the_log(self):
        from app.internet_intelligence.map.pipeline import regrade

        entity = self.store.upsert_entity("bgscet", name="BGSCET")
        domain, _ = self.store.upsert_asset("bgscet", asset_ref("https://bgscet.ac.in/"), entity_id=entity, relation="official")
        filler, _ = self.store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/filler/"), entity_id=entity)
        self.store.add_evidence("bgscet", asset_id=domain, kind="configured_domain", observed_via="reviewer")
        with self.store.batch("bgscet"):
            for _ in range(20_001):
                self.store.add_evidence("bgscet", asset_id=filler, kind="search_snippet", channel="t", observed_via="index")
        self.store.add_evidence("bgscet", asset_id=domain, kind="integrity", polarity="refutes", detail="hijacked:gambling_terms", source_url="https://bgscet.ac.in/")
        regrade(self.store, "bgscet")
        self.assertEqual((self.store.get_asset("bgscet", domain)["grade"], self.store.get_asset("bgscet", domain)["status"]), ("D", "hijacked"))

    def test_holdout_is_deterministic(self):
        self.assertEqual(in_holdout("instagram:bgscet_engg_coll", 20), in_holdout("instagram:bgscet_engg_coll", 20))
        self.assertFalse(in_holdout("anything", 0))
        keys = [f"web:site{n}.example" for n in range(1000)]
        share = sum(in_holdout(key, 20) for key in keys) / len(keys)
        self.assertTrue(0.15 < share < 0.25, share)

    def test_groups_filter_and_suppression(self):
        rows = parse_sweep(sweep(
            "BGSCET\tBGSCET\tinstitution\t\tBGSCET\thttps://www.instagram.com/bgscet_engg_coll/\tA\tofficial\tofficial",
            "SJBIT\tSJBIT\tinstitution\t\tSJBIT\thttps://www.instagram.com/sjbit_official\tA\tofficial\tofficial",
            "BGSCET\tBGSCET\tinstitution\t\tA student\thttps://www.instagram.com/some.student/\tF\tcommunity\tpersonal",
        ))
        self.store.suppress("bgscet", "instagram:some.student", reason="personal account")
        summary = import_seed(self.store, "bgscet", rows, groups=["bgscet"], holdout_percent=0)
        self.assertEqual((summary.assets_created, summary.skipped_groups, summary.suppressed), (1, 1, 1))

    def test_baseline_metrics_start_at_zero_and_move_with_grades(self):
        rows = parse_sweep(sweep(*[f"G\tInst {n}\tinstitution\t\tInst {n}\thttps://site{n}.example/\tA\tofficial\tofficial site" for n in range(40)]))
        lookalikes = parse_lookalikes("name\tnames\tlocations\turl\twhy\nParked\t\t\thttps://acmbgs.org/\tparked\n")
        import_seed(self.store, "bgscet", rows, lookalikes=lookalikes, holdout_percent=25)
        baseline = record_baseline(self.store, "bgscet")
        self.assertEqual(baseline["holdout_recall"], 0.0)
        self.assertEqual(baseline["seed_verification_rate"], 0.0)
        self.assertEqual(baseline["canary_leaks"], [])
        self.assertEqual(self.store.list_map_runs("bgscet", kind="baseline")[0]["metrics"]["assets"], baseline["assets"])
        # The map later finds a held-out site and verifies it: recall moves.
        holdout_key = sorted(self.store.holdout_keys("bgscet"))[0]
        found_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://" + holdout_key.removeprefix("web:") + "/"), entity_id=None, relation="official")
        self.store.set_grade("bgscet", found_id, grade="A", reasons=["official link"], scorer_version="t")
        self.assertGreater(map_metrics(self.store, "bgscet")["holdout_recall"], 0)
        # A canary graded official at B or better is a leak.
        canary_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://acmbgs.org/"), entity_id=None, relation="official")
        self.store.set_grade("bgscet", canary_id, grade="B", reasons=["mistake"], scorer_version="t")
        self.assertEqual([leak["asset_key"] for leak in map_metrics(self.store, "bgscet")["canary_leaks"]], ["web:acmbgs.org"])


class MapServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.service = MapService(self.store, seed_groups={"bgscet": ["BGSCET"]})
        self.manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        self.reader = principal(PrincipalType.FACULTY, college_id="bgscet")

    def test_importing_other_institutions_needs_a_named_approval(self):
        text = DEFAULT_SWEEP.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.seed(self.manager, "bgscet", sweep_text=text, all_groups=True)
        with self.assertRaises(ValueError):
            self.service.seed(self.manager, "bgscet", sweep_text=text)
        with self.assertRaises(PermissionError):
            self.service.seed(self.reader, "bgscet", sweep_text=text, groups=["BGSCET"])
        with self.assertRaises(PermissionError):
            self.service.seed(self.manager, "sjbit", sweep_text=text, groups=["SJBIT"])
        # Naming another institution's groups is not a way round the approval.
        with self.assertRaisesRegex(ValueError, "approved_by"):
            self.service.seed(self.manager, "bgscet", sweep_text=text, groups=["BGSCET", "SJBIT", "Math"])
        # Nor is a sweep file that labels other institutions' rows as one's own group.
        relabelled = sweep("BGSCET\tSJB Institute of Technology\tinstitution\t\t\thttps://www.instagram.com/sjbit_official/\tA\tofficial\t")
        with self.assertRaisesRegex(ValueError, "approved_by"):
            self.service.seed(self.manager, "bgscet", sweep_text=relabelled, groups=["BGSCET"])
        result = self.service.seed(self.manager, "bgscet", sweep_text=text, groups=["BGSCET"])
        self.assertGreater(result["summary"]["assets_created"], 10)
        self.assertEqual((result["approved_by"], result["needed_approval"], result["summary"]["groups"]), (None, False, ["BGSCET"]))
        self.assertIn("holdout_recall", result["baseline"])
        approved = self.service.seed(self.manager, "bgscet", sweep_text=text, all_groups=True, approved_by="Math IT office, 2026-09-30")
        self.assertTrue(approved["needed_approval"])
        record = self.store.list_map_runs("bgscet", kind="baseline")[0]["counts"]["import"]
        self.assertEqual((record["imported_by"], record["approved_by"], record["all_groups"]), (self.manager.principal_id, "Math IT office, 2026-09-30", True))
        self.assertGreater(len(record["groups"]), 5)
        unconfigured = MapService(self.store)
        with self.assertRaisesRegex(ValueError, "none configured"):
            unconfigured.seed(self.manager, "bgscet", sweep_text=text, groups=["BGSCET"])

    def test_a_seed_import_is_capped(self):
        from app.internet_intelligence.map.service import MAX_SEED_ROWS

        rows = sweep(*(f"BGSCET\tBGSCET\tinstitution\t\t\thttps://site{index}.example/\tC\tunknown\t" for index in range(MAX_SEED_ROWS + 1)))
        with self.assertRaisesRegex(ValueError, "at most"):
            self.service.seed(self.manager, "bgscet", sweep_text=rows, groups=["BGSCET"], approved_by="x")

    def test_readers_see_only_what_the_map_stands_behind(self):
        entity = self.store.upsert_entity("bgscet", name="BGSCET")
        verified, _ = self.store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=entity, relation="official")
        self.store.set_grade("bgscet", verified, grade="A", reasons=["official"], scorer_version="t")
        self.store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/bgscet_cse/"), entity_id=entity, relation="unknown")
        graded = {}
        for url, relation, grade in (
            ("https://www.bgsgroupofinstitutions.com/", "official", "D"), ("https://www.instagram.com/bgscet_claimed/", "official", "C"),
            ("https://www.reddit.com/r/bgscet/", "community", "C"), ("https://www.instagram.com/bgscet_mba/", "official", "B"),
        ):
            asset_id, _ = self.store.upsert_asset("bgscet", asset_ref(url), entity_id=entity, relation=relation)
            self.store.set_grade("bgscet", asset_id, grade=grade, reasons=["test"], scorer_version="t")
            graded[(relation, grade)] = asset_id
        shown = {asset["asset_id"] for asset in self.service.assets(self.reader, "bgscet")}
        self.assertEqual(shown, {verified, graded[("official", "B")], graded[("community", "C")]}, "readers never see refuted, parked or unverified official claims")
        self.assertEqual(len(self.service.assets(self.manager, "bgscet")), 6)
        exported = self.service.export(self.reader, "bgscet")
        self.assertIn("instagram.com/bgscet_mba", exported)
        for hidden in ("bgsgroupofinstitutions.com", "bgscet_claimed", "reddit.com/r/bgscet", "bgscet_cse"):
            self.assertNotIn(hidden, exported, hidden)
        with self.assertRaises(PermissionError):
            self.service.evidence(self.reader, "bgscet", verified)
        with self.assertRaises(PermissionError):
            self.service.assets(principal(PrincipalType.STUDENT, college_id="bgscet"), "bgscet")


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class MapRouteTests(unittest.TestCase):
    def test_routes_seed_list_and_measure(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "map-principal", "X-Demo-Role": "principal", "X-Demo-College": "map_college"}
        self.assertEqual(client.get("/v1/intelligence/map/assets", headers=headers).status_code, 503, "the map is off unless enabled")
        platform = app.state.runtime.platform
        service = MapService(MapStore(backend=platform.store.backend, suppression_key=b"test-key"))
        with mock.patch.object(platform, "intelligence_map", service):
            self.assertEqual(client.post("/v1/intelligence/map/seed", headers=headers, json={"groups": ["BGSCET"]}).status_code, 422, "no group is this college's own until an operator says so")
            seeded = client.post("/v1/intelligence/map/seed", headers=headers, json={"groups": ["BGSCET"], "approved_by": "Principal, map college"})
            self.assertEqual(seeded.status_code, 200, seeded.text)
            self.assertGreater(seeded.json()["summary"]["assets_created"], 10)
            refused = client.post("/v1/intelligence/map/seed", headers=headers, json={"all_groups": True})
            self.assertEqual(refused.status_code, 422)
            listed = client.get("/v1/intelligence/map/assets", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertTrue(listed.json()["assets"])
            first = listed.json()["assets"][0]["asset_id"]
            self.assertEqual(client.get(f"/v1/intelligence/map/assets/{first}/evidence", headers=headers).status_code, 200)
            metrics = client.get("/v1/intelligence/map/metrics", headers=headers).json()
            self.assertIn("holdout_recall", metrics)
            self.assertEqual(metrics["runs"][0]["kind"], "baseline")
            self.assertTrue(client.get("/v1/intelligence/map/entities", headers=headers).json()["entities"])
            student = {**headers, "X-Demo-Principal": "map-student", "X-Demo-Role": "student"}
            self.assertEqual(client.get("/v1/intelligence/map/assets", headers=student).status_code, 403)
            self.assertIn("intelligence.map.seed", {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)})
            no_profile = client.post("/v1/intelligence/map/sync-profile", headers=headers, json={})
            self.assertEqual(no_profile.status_code, 422, "the profile must exist first")
            client.put("/v1/intelligence/profile", headers=headers, json={"name": "Map College", "location": "Bengaluru", "official_domains": ["map-college.example"]})
            synced = client.post("/v1/intelligence/map/sync-profile", headers=headers, json={})
            self.assertEqual(synced.status_code, 200, synced.text)
            self.assertEqual(len(synced.json()["domains_added"]), 1)
            self.assertEqual(client.post("/v1/intelligence/map/harvest", headers=headers, json={}).status_code, 422, "no fetcher configured")
            regraded = client.post("/v1/intelligence/map/regrade", headers=headers, json={})
            self.assertEqual(regraded.status_code, 200)
            exported = client.get("/v1/intelligence/map/export", headers=headers)
            self.assertEqual(exported.status_code, 200)
            self.assertIn("text/tab-separated-values", exported.headers["content-type"])
            self.assertIn("map-college.example", exported.text)
            self.assertEqual(client.post("/v1/intelligence/map/regrade", headers={**headers, "X-Demo-Role": "faculty"}, json={}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
