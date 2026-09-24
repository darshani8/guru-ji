"""Retention of intelligence data (India's DPDP Act): what goes, what stays, and that no grade moves."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.api.dependencies import build_runtime
from app.api.platform_runtime import PlatformRuntime
from app.config.settings import AppSettings
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.grading import (
    EXPIRED_DETAIL,
    FREE_TEXT_KINDS,
    expire_quotes,
    grade,
    superseded_observations,
)
from app.internet_intelligence.map.incidents import IncidentDesk
from app.internet_intelligence.map.pipeline import regrade
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.redaction import strip_person_names
from app.internet_intelligence.store import IntelligenceStore
from app.workers.handlers import register_handlers

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
DAYS = 365
OLD = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=10)
PERSONAL = "bing: Ravi Kumar (a student) posted this on his own page"


def at(moment: datetime, **delta: float) -> str:
    return (moment + timedelta(**delta)).isoformat()


def backdate(store, table: str, key: str, value: str, **columns: str) -> None:
    """Move a row's timestamps into the past (SQLite has no row-level security to pin)."""

    with store.backend.transaction():
        store.backend.execute(f"UPDATE {table} SET {', '.join(f'{column} = ?' for column in columns)} WHERE {key} = ?", (*columns.values(), value))


def count(store, table: str, institution_id: str, where: str = "1 = 1", params: tuple = ()) -> int:
    with store.backend.transaction(tenant_id=institution_id):
        return int(store.backend.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE institution_id = ? AND {where}", (institution_id, *params))["n"])


def fill_map(store: MapStore, institution: str, now: datetime = NOW) -> dict[str, str]:
    """One old and one recent row of everything map retention looks at; returns the ids the tests follow."""

    old, recent = now - timedelta(days=400), now - timedelta(days=10)
    ids: dict[str, str] = {}
    for name, when, status in (("old_decided", old, "decided"), ("old_expired", old, "expired"), ("old_open", old, "open"), ("recent_decided", recent, "decided")):
        ids[name], _ = store.add_review_item(institution, kind="candidate_account", title=name, url=f"https://www.instagram.com/{name}/", now=when.isoformat())
        if status == "decided":
            store.decide_review_item(institution, ids[name], decision="dismiss", decided_by="p", note=f"{name}: someone's note")
        backdate(store, "intel_review_items", "review_id", ids[name], status=status, updated_at=when.isoformat(), **({"decided_at": when.isoformat()} if status == "decided" else {}))
    for name, when, resolve in (("old_resolved", old, True), ("old_open_incident", old, False), ("recent_resolved", recent, True)):
        row, _ = store.upsert_incident(institution, kind="site_suspect", target=f"{name}.in", severity="medium", title=name, now=when.isoformat())
        ids[name] = row["incident_id"]
        if resolve:
            store.set_incident_status(institution, ids[name], status="resolved", by="p")
            backdate(store, "intel_incidents", "incident_id", ids[name], resolved_at=when.isoformat())
    for name, kind, when in (("old_tick", "tick", old), ("older_baseline", "baseline", old - timedelta(days=5)), ("old_baseline", "baseline", old), ("recent_tick", "tick", recent)):
        ids[name] = store.start_map_run(institution, kind=kind)
        store.finish_map_run(institution, ids[name], status="succeeded")
        backdate(store, "intel_map_runs", "run_id", ids[name], started_at=when.isoformat())
    for when in (now - timedelta(days=40), now - timedelta(days=5)):
        store.reserve_budget(institution, connector="fetch", units=1, day=when.date().isoformat(), global_cap=100, tenant_cap=100)
    for name, when in (("https://old.example/page", old), ("https://recent.example/page", recent)):
        store.record_fetch(institution, name, outcome="ok", etag="e", last_modified=None, content_sha256=None)
        backdate(store, "intel_fetch_validators", "url_sha256", hashlib.sha256(name.encode("utf-8")).hexdigest(), fetched_at=when.isoformat())
    for name, when, status in (("old_expired_lead", old, "expired"), ("old_pruned_lead", old, "pruned"), ("old_active_lead", old, "active"), ("recent_expired_lead", recent, "expired")):
        ids[name], _ = store.upsert_source(institution, connector="lead_page", target=f"https://{name}.example/", origin="lead", work_class="explore")
        if status != "active":
            store.set_source_status(institution, ids[name], status)
        backdate(store, "intel_sources", "source_id", ids[name], updated_at=when.isoformat())
    domain, _ = store.upsert_asset(institution, asset_ref("https://bgscet.ac.in/"), entity_id=None, relation="official")
    account, _ = store.upsert_asset(institution, asset_ref("https://www.instagram.com/bgscet_engg_coll/"), entity_id=None, relation="official")
    ids["old_snippet"] = store.add_evidence(institution, asset_id=account, kind="search_snippet", detail=PERSONAL, source_url="https://www.instagram.com/bgscet_engg_coll/", channel="search:bing", observed_via="index", observed_at=old.isoformat())
    ids["recent_snippet"] = store.add_evidence(institution, asset_id=account, kind="search_snippet", detail="bing: BGSCET on Instagram", channel="search:bing", observed_via="index", observed_at=recent.isoformat())
    ids["old_link"] = store.add_evidence(institution, asset_id=account, kind="official_link", detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=domain, channel="site:bgscet.ac.in", observed_at=old.isoformat())
    ids["account"] = account
    return ids


class MapRetentionTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def ids_in(self, table: str, key: str, institution: str) -> set[str]:
        with self.store.backend.transaction(tenant_id=institution):
            return {str(row[key]) for row in self.store.backend.fetchall(f"SELECT {key} FROM {table} WHERE institution_id = ?", (institution,))}

    def test_closed_work_past_the_period_goes_and_everything_else_stays(self):
        ids = fill_map(self.store, "bgscet")
        counts = self.store.prune_retention(now=NOW, days=DAYS, institution_id="bgscet")
        self.assertEqual(
            {key: counts[key] for key in ("review_items", "incidents", "map_runs", "quota", "fetch_validators", "sources", "evidence_blanked", "budget_ledger", "institutions")},
            {"review_items": 2, "incidents": 1, "map_runs": 2, "quota": 1, "fetch_validators": 1, "sources": 2, "evidence_blanked": 1, "budget_ledger": 1, "institutions": 1},
        )
        self.assertEqual(self.ids_in("intel_review_items", "review_id", "bgscet"), {ids["old_open"], ids["recent_decided"]}, "an open item waits for a person whatever its age")
        self.assertEqual(self.ids_in("intel_incidents", "incident_id", "bgscet"), {ids["old_open_incident"], ids["recent_resolved"]})
        self.assertEqual(self.ids_in("intel_map_runs", "run_id", "bgscet"), {ids["old_baseline"], ids["recent_tick"]}, "the newest baseline stays for comparison")
        self.assertEqual(self.ids_in("intel_sources", "source_id", "bgscet"), {ids["old_active_lead"], ids["recent_expired_lead"]})
        self.assertEqual(count(self.store, "intel_fetch_validators", "bgscet"), 1)
        self.assertEqual(list(self.store.tenant_spend("bgscet", day=(NOW - timedelta(days=5)).date().isoformat())), ["fetch"], "the recent quota row stays")
        self.assertEqual(self.store.spend(day=(NOW - timedelta(days=40)).date().isoformat()), {}, "the platform-wide ledger is pruned too")
        evidence = {row["evidence_id"]: row for row in self.store.list_evidence("bgscet", asset_id=ids["account"])}
        old = evidence[ids["old_snippet"]]
        self.assertEqual((old["detail"], old["kind"], old["polarity"], old["channel"], old["observed_via"], old["source_url"]), (EXPIRED_DETAIL, "search_snippet", "supports", "search:bing", "index", "https://www.instagram.com/bgscet_engg_coll/"))
        self.assertEqual(evidence[ids["recent_snippet"]]["detail"], "bing: BGSCET on Instagram")
        self.assertEqual(evidence[ids["old_link"]]["detail"], "A:footer", "a detail the grader parses is never blanked")
        self.assertNotIn("Ravi Kumar", str(self.store.backend.fetchall("SELECT * FROM intel_evidence")))
        again = self.store.prune_retention(now=NOW, days=DAYS, institution_id="bgscet")
        self.assertEqual(sum(value for key, value in again.items() if key != "institutions"), 0, "a second pass finds nothing")

    def test_the_period_is_positive_and_quota_rows_go_within_a_month(self):
        with self.assertRaises(ValueError):
            self.store.prune_retention(now=NOW, days=0)
        self.store.reserve_budget("bgscet", connector="fetch", units=1, day=(NOW - timedelta(days=31)).date().isoformat(), global_cap=10, tenant_cap=10)
        self.assertEqual(self.store.prune_retention(now=NOW, days=3650)["quota"], 1, "a quota row only counts on its own day")

    def test_pruning_one_institution_leaves_another_alone(self):
        first, second = fill_map(self.store, "first"), fill_map(self.store, "second")
        before = {table: count(self.store, table, "second") for table in ("intel_review_items", "intel_incidents", "intel_map_runs", "intel_sources", "intel_quota", "intel_fetch_validators", "intel_evidence")}
        self.store.prune_retention(now=NOW, days=DAYS, institution_id="first")
        self.assertEqual({table: count(self.store, table, "second") for table in before}, before)
        # Untouched: still the text as stored (people's names were already taken out on the way in), not blanked.
        self.assertEqual({row["evidence_id"]: row["detail"] for row in self.store.list_evidence("second", asset_id=second["account"])}[second["old_snippet"]], strip_person_names(PERSONAL))
        self.assertNotIn(first["old_decided"], self.ids_in("intel_review_items", "review_id", "first"))
        # With none named, every institution that has rows is pruned, each in its own tenant transaction.
        counts = self.store.prune_retention(now=NOW, days=DAYS)
        self.assertEqual((counts["institutions"], counts["review_items"]), (2, 2))
        self.assertEqual(self.ids_in("intel_review_items", "review_id", "second"), {second["old_open"], second["recent_decided"]})

    def test_grades_and_reasons_replay_the_same_after_pruning(self):
        store, inst = self.store, "bgscet"
        entity = store.upsert_entity(inst, name="BGS College of Engineering and Technology")

        def asset(url: str, relation: str = "official") -> str:
            return store.upsert_asset(inst, asset_ref(url), entity_id=entity, relation=relation)[0]

        def evidence(asset_id: str, kind: str, when: datetime, **fields) -> None:
            store.add_evidence(inst, asset_id=asset_id, kind=kind, observed_at=when.isoformat(), **fields)

        with store.batch(inst):
            domain = asset("https://bgscet.ac.in/")
            evidence(domain, "configured_domain", OLD - timedelta(days=100), detail="profile of BGSCET", channel="profile", observed_via="reviewer")
            for day in range(0, 730, 3):
                when = NOW - timedelta(days=730 - day)
                failed = day % 90 == 0
                evidence(domain, "liveness", when, polarity="refutes" if failed else "supports", detail="blocked:403" if failed else "ok:200", channel="fetch")
                evidence(domain, "integrity", when, detail="clean:", channel="site:bgscet.ac.in")
            # An account the official site links from its footer (A), with old snippets.
            instagram = asset("https://www.instagram.com/bgscet_engg_coll/")
            evidence(instagram, "official_link", OLD, detail="A:footer", source_url="https://bgscet.ac.in/", source_asset_id=domain, channel="site:bgscet.ac.in")
            evidence(instagram, "search_snippet", OLD, detail="bing: BGSCET (@bgscet_engg_coll) - photos of our students", channel="search:bing", observed_via="index")
            # An account linked from an A-graded hub (B).
            youtube = asset("https://www.youtube.com/@bgscet")
            evidence(youtube, "hub_link", OLD, detail="A:hub", source_url="https://linktr.ee/bgscet", channel="hub:bgscet")
            evidence(youtube, "api_identity", OLD, detail="youtube UC123: BGSCET official", channel="youtube_api")
            # One channel (C), two channels (B), a reviewer's rejection (D), an imported claim and a community record (C).
            single = asset("https://x.com/bgscet")
            evidence(single, "search_snippet", OLD, detail="bing: Priya Sharma's thread about BGSCET", channel="search:bing", observed_via="index")
            double = asset("https://www.linkedin.com/company/bgscet")
            evidence(double, "search_snippet", OLD, detail="bing: BGSCET | LinkedIn", channel="search:bing", observed_via="index")
            evidence(double, "search_snippet", OLD, detail="brave: BGSCET company page", channel="search:brave", observed_via="index")
            rejected = asset("https://www.instagram.com/a.student/", "unknown")
            evidence(rejected, "search_snippet", OLD, detail="bing: a.student on Instagram", channel="search:bing", observed_via="index")
            evidence(rejected, "reviewer_reject", OLD, polarity="refutes", detail="a student's own page, not the college", channel="reviewer:p", observed_via="reviewer")
            imported = asset("https://www.instagram.com/imported/")
            evidence(imported, "imported_claim", OLD, detail="claimed B: from the sweep, per Mr. Rao", channel="sweep", observed_via="import")
            evidence(imported, "community_record", OLD, detail="wikidata:Q123:P2003", channel="wikidata", observed_via="index")
            # A hijack stands until someone confirms the domain again, however many clean pages follow.
            hijacked = asset("https://old-bgscet.in/")
            evidence(hijacked, "configured_domain", OLD - timedelta(days=200), detail="profile", channel="profile", observed_via="reviewer")
            evidence(hijacked, "integrity", OLD - timedelta(days=100), polarity="refutes", detail="hijacked:gambling_terms", channel="site:old-bgscet.in")
            for day in range(1, 40):
                evidence(hijacked, "integrity", OLD - timedelta(days=100 - day), detail="clean:", channel="site:old-bgscet.in")
            parked = asset("https://parked-bgscet.in/")
            evidence(parked, "configured_domain", OLD - timedelta(days=200), detail="profile", channel="profile", observed_via="reviewer")
            for day in range(20):
                evidence(parked, "integrity", OLD - timedelta(days=60 - day), detail="clean:", channel="dns")
            evidence(parked, "integrity", OLD, polarity="refutes", detail="parked:name servers sedo", channel="dns")
            # Dead: two not-found failures in one unbroken run, far apart, the middle of it old.
            dead = asset("https://www.facebook.com/bgscet/")
            evidence(dead, "search_snippet", OLD, detail="bing: BGSCET Facebook", channel="search:bing", observed_via="index")
            evidence(dead, "liveness", OLD - timedelta(days=100), detail="ok:200", channel="fetch")
            for day in range(0, 30, 2):
                evidence(dead, "liveness", OLD + timedelta(days=day), polarity="refutes", detail="not_found:404" if day % 4 == 0 else "blocked:403", channel="fetch")
            evidence(dead, "liveness", RECENT, polarity="refutes", detail="not_found:404", channel="feed")
            # Nearly dead: the old failure is cut off from the recent one by a success in between.
            nearly = asset("https://www.facebook.com/nearlydead/")
            evidence(nearly, "search_snippet", OLD, detail="bing: nearly", channel="search:bing", observed_via="index")
            evidence(nearly, "liveness", OLD - timedelta(days=10), detail="ok:200", channel="fetch")
            evidence(nearly, "liveness", OLD, polarity="refutes", detail="not_found:404", channel="fetch")
            evidence(nearly, "liveness", OLD + timedelta(days=1), detail="ok:200", channel="fetch")
            for day in range(2, 40):
                evidence(nearly, "liveness", OLD + timedelta(days=day), polarity="refutes", detail="timeout:0", channel="fetch")
            evidence(nearly, "liveness", RECENT, polarity="refutes", detail="not_found:404", channel="fetch")
            walled = asset("https://www.instagram.com/walled/")
            evidence(walled, "search_snippet", OLD, detail="bing: walled", channel="search:bing", observed_via="index")
            for day in range(0, 700, 7):
                evidence(walled, "liveness", NOW - timedelta(days=700 - day), polarity="refutes", detail="login_wall:200", channel="fetch")

        def snapshot() -> dict[str, tuple]:
            return {row["asset_id"]: (row["grade"], row["status"], row["last_verified_at"], tuple(row["grade_reasons"])) for row in store.iter_assets(inst)}

        regrade(store, inst, now=NOW)
        before = snapshot()
        expected = {domain: "A", instagram: "A", youtube: "B", single: "C", double: "B", rejected: "D", imported: "C", hijacked: "D", parked: "D", dead: "D", nearly: "C", walled: "C"}
        self.assertEqual({asset_id: before[asset_id][0] for asset_id in expected}, expected)
        self.assertEqual((before[hijacked][1], before[parked][1], before[dead][1], before[walled][1]), ("hijacked", "parked", "dead", "blocked"))
        counts = store.prune_retention(now=NOW, days=DAYS)
        self.assertGreater(counts["observations"], 200)
        self.assertEqual(counts["evidence_blanked"], 12)
        pruned = snapshot()
        self.assertEqual(regrade(store, inst, now=NOW), [], "no grade changes")
        after = snapshot()
        self.assertEqual({key: value[:3] for key, value in after.items()}, {key: value[:3] for key, value in before.items()}, "grades, statuses and verification times replay the same")
        self.assertEqual(after, pruned, "the stored reasons were already worded as a regrade words them")
        self.assertEqual(after[single][3], (f"search snippet ({EXPIRED_DETAIL})",))
        self.assertEqual(after[rejected][3], (f"reviewer_reject: {EXPIRED_DETAIL}",))
        for text in ("Priya Sharma", "a student's own page", "Mr. Rao", "photos of our students"):
            self.assertNotIn(text, str(store.backend.fetchall("SELECT * FROM intel_evidence")) + str(store.backend.fetchall("SELECT grade_reasons_json FROM intel_assets")))
        self.assertEqual(count(store, "intel_evidence", inst, "kind = 'integrity' AND detail LIKE 'hijacked%'"), 1, "the hijack verdict is kept")


class GradingRetentionRuleTests(unittest.TestCase):
    def test_blanking_free_text_never_changes_a_grade(self):
        asset = {"asset_id": "a", "kind": "account", "grade": "unrated"}
        other = {"evidence_id": "e0", "kind": "search_snippet", "polarity": "supports", "detail": "brave: other", "channel": "search:brave", "observed_via": "index", "observed_at": at(OLD, days=-1)}
        for kind in sorted(FREE_TEXT_KINDS):
            for polarity in ("supports", "refutes"):
                for via in ("index", "reviewer", "live", "import"):
                    for text in ("confirm", "Ravi Kumar (a student) wrote this, a long free-text note about the account " * 3):
                        row = {"evidence_id": "e1", "kind": kind, "polarity": polarity, "detail": text, "channel": "c1", "observed_via": via, "observed_at": at(OLD)}
                        full, blank = grade(asset, [other, row], now=NOW), grade(asset, [other, {**row, "detail": EXPIRED_DETAIL}], now=NOW)
                        self.assertEqual((full.grade, full.status, full.verified_at), (blank.grade, blank.status, blank.verified_at), (kind, polarity, via))
                        self.assertEqual(expire_quotes(full.reasons, [text]), blank.reasons, (kind, polarity, via))

    def test_only_the_quote_at_the_end_of_a_reason_is_replaced(self):
        self.assertEqual(expire_quotes(["confirmed by a reviewer (confirm)", "official domain configured by the institution"], ["confirm"]), [f"confirmed by a reviewer ({EXPIRED_DETAIL})", "official domain configured by the institution"])
        self.assertEqual(expire_quotes(["search snippet (bing: a)", "search snippet (bing: b)"], ["bing: a", "bing: b"]), [f"search snippet ({EXPIRED_DETAIL})"], "worded and de-duplicated as grade() would")

    def test_pruned_observations_never_change_a_grade(self):
        rng = random.Random(20260923)
        liveness = [("supports", "ok:200"), ("refutes", "not_found:404"), ("refutes", "gone:410"), ("refutes", "blocked:403"), ("refutes", "login_wall:200"), ("refutes", "timeout:0"), ("refutes", "not_found:nxdomain")]
        integrity = [("supports", "clean:"), ("refutes", "compromised:hidden_links"), ("refutes", "hijacked:gambling_terms"), ("refutes", "parked:sedo"), ("refutes", "redirects_offsite:other.in"), ("refutes", "suspect:links")]
        before, doomed_total = NOW - timedelta(days=DAYS), 0
        for case in range(600):
            asset = {"asset_id": f"a{case}", "kind": rng.choice(["domain", "account"]), "grade": "unrated"}
            rows = []
            for index in range(rng.randint(1, 40)):
                kind = rng.choice(["liveness", "liveness", "integrity"])
                polarity, detail = rng.choice(liveness if kind == "liveness" else integrity)
                rows.append({
                    "evidence_id": f"e{index:03d}", "asset_id": asset["asset_id"], "kind": kind, "polarity": polarity, "detail": detail, "channel": rng.choice(["fetch", "feed", "dns", "site:x.in"]),
                    "observed_via": rng.choice(["live", "live", "index"]), "observed_at": at(NOW, days=-rng.randint(0, 800), hours=-rng.randint(0, 47)), "source_url": "", "source_asset_id": None,
                })
            if rng.random() < 0.6:
                rows.append({"evidence_id": "z1", "asset_id": asset["asset_id"], "kind": "configured_domain", "polarity": "supports", "detail": "profile", "channel": "profile", "observed_via": "reviewer", "observed_at": at(NOW, days=-900)})
            if rng.random() < 0.3:
                rows.append({"evidence_id": "z2", "asset_id": asset["asset_id"], "kind": rng.choice(["owner_claim", "reviewer_confirm"]), "polarity": "supports", "detail": "x", "channel": "owner:x.in", "observed_via": rng.choice(["owner", "reviewer"]), "observed_at": at(NOW, days=-rng.randint(0, 800))})
            doomed = set(superseded_observations(rows, before=before))
            self.assertFalse({row["evidence_id"] for row in rows if datetime.fromisoformat(row["observed_at"]) >= before} & doomed, "nothing inside the period goes")
            full, kept = grade(asset, rows, now=NOW), grade(asset, [row for row in rows if row["evidence_id"] not in doomed], now=NOW)
            self.assertEqual((full.grade, full.reasons, full.status, full.verified_at, full.verified_via), (kept.grade, kept.reasons, kept.status, kept.verified_at, kept.verified_via), rows)
            doomed_total += len(doomed)
        self.assertGreater(doomed_total, 2000, "pruning does remove superseded rows")


def document(url: str) -> dict:
    return {
        "canonical_url": url, "url": url, "domain": "news.example", "source_type": "news", "title": f"About {url}", "excerpt": "A person was named here.", "content_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "match_level": "strong", "match_score": 0.9, "status": "kept",
    }


def fill_monitoring(store: IntelligenceStore, institution: str, now: datetime = NOW) -> dict[str, str]:
    old, recent = now - timedelta(days=400), now - timedelta(days=10)
    store.save_profile(InstitutionProfile(institution, f"College {institution}"), updated_by="p")
    ids: dict[str, str] = {}
    for name, when in (("old", old), ("recent", recent)):
        ids[f"{name}_document"], _ = store.upsert_document(institution, document(f"https://news.example/{institution}/{name}"))
        backdate(store, "internet_documents", "document_id", ids[f"{name}_document"], last_seen_at=when.isoformat())
        ids[f"{name}_run"] = store.try_start_run(institution)
        store.finish_run(institution, ids[f"{name}_run"], status="succeeded", new_count=1, changed_count=0, duplicate_count=0, excluded_count=0)
        backdate(store, "monitoring_runs", "run_id", ids[f"{name}_run"], started_at=when.isoformat())
        ids[f"{name}_event"] = store.add_event(institution, run_id=ids[f"{name}_run"], document_id=ids[f"{name}_document"], event_type="new", importance_level="high", source_type="news", title="t", url="u", summary="s")
        backdate(store, "monitoring_events", "event_id", ids[f"{name}_event"], created_at=when.isoformat())
        ids[f"{name}_report"] = store.save_report(institution, requested_by="p", question="what is said about Ravi?", window_days=7, summary="s", findings=[{"title": "t"}])
        backdate(store, "intelligence_reports", "report_id", ids[f"{name}_report"], created_at=when.isoformat())
    for when in (now - timedelta(days=40), now - timedelta(days=5)):
        store.take_investigation(institution, "p1", day=when.date().isoformat(), cap=5)
    return ids


class MonitoringRetentionTests(unittest.TestCase):
    def setUp(self):
        self.store = IntelligenceStore(":memory:")

    def test_old_monitoring_evidence_goes_and_recent_stays(self):
        ids = fill_monitoring(self.store, "bgscet")
        counts = self.store.prune_retention(DAYS, now=NOW, institution_id="bgscet")
        self.assertEqual(counts, {"internet_documents": 1, "monitoring_events": 1, "monitoring_runs": 1, "intelligence_reports": 1, "investigation_quota": 1, "institutions": 1})
        self.assertIsNone(self.store.get_document("bgscet", ids["old_document"]))
        self.assertIsNotNone(self.store.get_document("bgscet", ids["recent_document"]))
        self.assertEqual([run["run_id"] for run in self.store.list_runs("bgscet")], [ids["recent_run"]])
        self.assertEqual([report["report_id"] for report in self.store.list_reports("bgscet")], [ids["recent_report"]])
        self.assertEqual(count(self.store, "monitoring_events", "bgscet"), 1)
        self.assertEqual(self.store.investigations_used("bgscet", "p1", day=(NOW - timedelta(days=5)).date().isoformat()), 1)
        self.assertIsNotNone(self.store.get_profile("bgscet"), "profiles stay")
        with self.assertRaises(ValueError):
            self.store.prune_retention(0)

    def test_pruning_one_institution_leaves_another_alone(self):
        fill_monitoring(self.store, "first")
        second = fill_monitoring(self.store, "second")
        self.store.prune_retention(DAYS, now=NOW, institution_id="first")
        self.assertIsNotNone(self.store.get_document("second", second["old_document"]))
        self.assertEqual(count(self.store, "intelligence_reports", "second"), 2)
        counts = self.store.prune_retention(DAYS, now=NOW)
        self.assertEqual((counts["institutions"], counts["internet_documents"]), (2, 1))
        self.assertIsNone(self.store.get_document("second", second["old_document"]))
        self.assertIsNotNone(self.store.get_document("second", second["recent_document"]))


class RetentionSettingsTests(unittest.TestCase):
    def test_the_period_is_between_30_and_3650_days(self):
        self.assertEqual(AppSettings().intelligence_retention_days, 365)
        for days in (30, 365, 3650):
            AppSettings(intelligence_retention_days=days).ensure_safe_for_production()
        for days in (0, 29, 3651):
            with self.assertRaisesRegex(ValueError, "SAFFRON_INTELLIGENCE_RETENTION_DAYS", msg=str(days)):
                AppSettings(intelligence_retention_days=days).ensure_safe_for_production()

    def test_it_is_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {"SAFFRON_INTELLIGENCE_RETENTION_DAYS": "90"}):
            self.assertEqual(AppSettings.from_env().intelligence_retention_days, 90)

    def test_the_example_environment_documents_it(self):
        self.assertIn("SAFFRON_INTELLIGENCE_RETENTION_DAYS=365", (REPO_ROOT / ".env.example").read_text(encoding="utf-8"))


class Queue:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler

    def register_failure_hook(self, name, hook):
        return None


class RetentionHookTests(unittest.TestCase):
    def settings(self, path: Path) -> AppSettings:
        return AppSettings(environment="test", control_database_url=":memory:", institution_database_url=f"sqlite:///{path}", object_store_backend="memory", intelligence_map_enabled=True)

    def test_start_up_and_the_digest_jobs_prune_both_stores(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "institution.db"
            monitoring, store = IntelligenceStore(f"sqlite:///{path}"), MapStore(f"sqlite:///{path}", suppression_key=b"k")
            documents, reviews = fill_monitoring(monitoring, "bgscet", now=now), fill_map(store, "bgscet", now=now)
            monitoring.close()
            store.close()
            runtime = build_runtime(self.settings(path))
            try:
                platform = runtime.platform
                self.assertIsNone(platform.intelligence_store.get_document("bgscet", documents["old_document"]), "start-up pruned the monitoring store")
                self.assertIsNotNone(platform.intelligence_store.get_document("bgscet", documents["recent_document"]))
                self.assertIsNone(platform.intelligence_map.store.get_review_item("bgscet", reviews["old_decided"]), "start-up pruned the map")
                self.assertIsNotNone(platform.intelligence_map.store.get_review_item("bgscet", reviews["recent_decided"]))
                # The digest job, as the worker registers it, applies the period for its institution.
                result = asyncio.run(platform.jobs._handlers["intelligence.map_digest"]({"institution_id": "bgscet"}))
                self.assertEqual(set(result["pruned"]), {"monitoring", "map"})
                self.assertEqual(result["pruned"]["map"]["institutions"], 1)
                # So does the scheduled script's --digest pass.
                spec = importlib.util.spec_from_file_location("run_monitor_for_tests", REPO_ROOT / "scripts" / "run_monitor.py")
                script = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(script)
                [digest] = asyncio.run(script._run(runtime, "bgscet", digest=True))
                self.assertEqual((set(digest["pruned"]), digest["pruned"]["monitoring"]["institutions"]), ({"monitoring", "map"}, 1))
                self.assertNotIn("incidents", digest)
            finally:
                runtime.close()

    def test_a_failing_prune_never_blocks_start_up(self):
        with mock.patch.object(PlatformRuntime, "prune_intelligence", side_effect=RuntimeError("lock timeout")):
            with self.assertLogs("app.api.dependencies", level="WARNING") as logs:
                runtime = build_runtime(AppSettings(environment="test", control_database_url=":memory:", object_store_backend="memory"))
        try:
            self.assertIsNotNone(runtime.platform)
            self.assertIn("intelligence retention pruning skipped at start-up: lock timeout", "".join(logs.output))
        finally:
            runtime.close()

    def test_the_digest_job_reports_a_failed_prune_instead_of_failing(self):
        desk = IncidentDesk(MapStore(":memory:", suppression_key=b"k"), recipients=lambda institution_id: ("p",), notify=lambda *args: None)
        desk.record("bgscet", [{"kind": "domain_not_resolving", "target": "old.bgscet.ac.in"}])
        calls: list[str] = []
        working, broken = Queue(), Queue()
        register_handlers(working, map_desk=desk, retention=lambda institution_id: calls.append(institution_id) or {"map": {"review_items": 0}})
        self.assertEqual(asyncio.run(working.handlers["intelligence.map_digest"]({"institution_id": "bgscet"}))["pruned"], {"map": {"review_items": 0}})
        self.assertEqual(calls, ["bgscet"])

        def fail(institution_id: str):
            raise RuntimeError("lock timeout")

        # Something new since the first digest, so the second one has something to send.
        desk.record("bgscet", [{"kind": "domain_not_resolving", "target": "exams.bgscet.ac.in"}])
        register_handlers(broken, map_desk=desk, retention=fail)
        with self.assertLogs("app.workers.handlers", level="WARNING"):
            result = asyncio.run(broken.handlers["intelligence.map_digest"]({"institution_id": "bgscet"}))
        self.assertEqual((result["sent"], result["pruned"]), (True, {"error": "lock timeout"}), "the digest already went out; a retry would send it twice")


if __name__ == "__main__":
    unittest.main()
