"""The internet map's memory: entities, assets, the evidence log and how it is measured.

Everything is tenant data under forced row-level security on PostgreSQL. The
evidence table is append-only (there is no update method): a grade is always
recomputed from it, so any grade can be explained and replayed. Two things
touch it afterwards. ``forget_asset``, for an account a reviewer found to be a
person's, removes it entirely, and only a keyed fingerprint remains.
``prune_retention`` (the retention period, for India's DPDP Act) blanks free
text the grader never parses and deletes observations that newer ones
superseded, so every grade still replays the same. Tables carry an ``intel_`` prefix because this store shares its database with
the institution-data store, which already owns generic names such as
``review_items``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from ...persistence.schema_tools import add_missing_columns, apply_schema, begin_migration, bound_lock_waits, existing_policies, idempotent_tenant_isolation_sql, known_tenants, row_level_security_state, tenant_isolation_statements
from ...persistence.sql_backend import SqlBackend, open_backend
from ..redaction import names_of, strip_person_names
from .assets import AssetRef, asset_ref

SCHEMA_VERSION = "004_intelligence_map"

ENTITY_KINDS = frozenset({"organisation", "trust", "institution", "unit", "branch", "role", "lookalike"})
RELATIONS = frozenset({"official", "affiliated", "community", "third_party", "unknown"})
# O: confirmed by the owner. A: live identity link from a healthy official
# page. A-arch: the same, but only in an archived copy. B: one step from an A
# anchor, or two independent channels. C: a single channel. D: refuted, dead,
# a lookalike, or on a parked or hijacked domain.
GRADES = ("O", "A", "A-arch", "B", "C", "D", "unrated")
GRADE_RANK = {"O": 6, "A": 5, "A-arch": 4, "B": 3, "C": 2, "unrated": 1, "D": 0}
ASSET_STATUSES = frozenset({"unknown", "live", "blocked", "dead", "parked", "hijacked", "compromised", "disputed", "redirected"})
SPLITS = frozenset({"seed", "holdout", "canary"})

_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS intel_entities (
        entity_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        names_json TEXT NOT NULL DEFAULT '[]',
        locations_json TEXT NOT NULL DEFAULT '[]',
        group_label TEXT NOT NULL DEFAULT '',
        parent_id TEXT,
        authority TEXT NOT NULL DEFAULT 'self',
        status TEXT NOT NULL DEFAULT 'active',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(institution_id, kind, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_entities_institution ON intel_entities(institution_id, kind)",
    """
    CREATE TABLE IF NOT EXISTS intel_assets (
        asset_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        entity_id TEXT,
        asset_key TEXT NOT NULL,
        platform TEXT NOT NULL,
        kind TEXT NOT NULL,
        url TEXT NOT NULL,
        handle TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT 'unknown',
        grade TEXT NOT NULL DEFAULT 'unrated',
        grade_reasons_json TEXT NOT NULL DEFAULT '[]',
        proposed_grade TEXT,
        scorer_version TEXT,
        status TEXT NOT NULL DEFAULT 'unknown',
        platform_id TEXT,
        followers INTEGER,
        followers_source TEXT,
        followers_at TEXT,
        last_verified_at TEXT,
        last_verified_via TEXT,
        note TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(institution_id, asset_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_assets_institution ON intel_assets(institution_id, platform, grade)",
    """
    CREATE TABLE IF NOT EXISTS intel_evidence (
        evidence_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        polarity TEXT NOT NULL DEFAULT 'supports',
        source_url TEXT NOT NULL DEFAULT '',
        source_asset_id TEXT,
        detail TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        observed_via TEXT NOT NULL DEFAULT 'live',
        run_id TEXT,
        raw_sha256 TEXT,
        observed_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_evidence_asset ON intel_evidence(institution_id, asset_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_intel_evidence_source ON intel_evidence(institution_id, source_asset_id, kind)",
    """
    CREATE TABLE IF NOT EXISTS intel_gold_items (
        gold_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        asset_key TEXT NOT NULL,
        platform TEXT NOT NULL,
        entity_name TEXT NOT NULL DEFAULT '',
        relation TEXT NOT NULL DEFAULT 'unknown',
        split TEXT NOT NULL,
        expected_min_grade TEXT NOT NULL DEFAULT 'C',
        source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(institution_id, asset_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intel_suppression (
        institution_id TEXT NOT NULL,
        key_hmac TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY(institution_id, key_hmac)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intel_map_runs (
        run_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'tick',
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        stop_reason TEXT,
        gate TEXT,
        spend_json TEXT NOT NULL DEFAULT '{}',
        counts_json TEXT NOT NULL DEFAULT '{}',
        metrics_json TEXT NOT NULL DEFAULT '{}',
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_map_runs_institution ON intel_map_runs(institution_id, started_at)",
    # At most one running tick per institution, enforced by the database: two
    # workers racing past the NOT EXISTS check cannot both insert.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_map_runs_one_tick ON intel_map_runs(institution_id, kind) WHERE status = 'running' AND kind = 'tick'",
    """
    CREATE TABLE IF NOT EXISTS intel_sources (
        source_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        connector TEXT NOT NULL,
        target TEXT NOT NULL,
        entity_id TEXT,
        asset_id TEXT,
        topic TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'seed',
        work_class TEXT NOT NULL DEFAULT 'rotation',
        hops INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        due_at TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL DEFAULT 86400,
        lease_owner TEXT,
        lease_until TEXT,
        etag TEXT,
        last_modified TEXT,
        last_outcome TEXT,
        failure_streak INTEGER NOT NULL DEFAULT 0,
        runs INTEGER NOT NULL DEFAULT 0,
        yield_total INTEGER NOT NULL DEFAULT 0,
        yield_last INTEGER NOT NULL DEFAULT 0,
        cost_total REAL NOT NULL DEFAULT 0,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(institution_id, connector, target)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_sources_due ON intel_sources(institution_id, status, due_at)",
    """
    CREATE TABLE IF NOT EXISTS intel_quota (
        institution_id TEXT NOT NULL,
        day TEXT NOT NULL,
        connector TEXT NOT NULL,
        units REAL NOT NULL DEFAULT 0,
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(institution_id, day, connector)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intel_review_items (
        review_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        severity TEXT NOT NULL DEFAULT 'normal',
        asset_id TEXT,
        entity_id TEXT,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        connector TEXT NOT NULL DEFAULT '',
        source_id TEXT,
        run_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        decision TEXT,
        decision_note TEXT,
        decided_by TEXT,
        decided_at TEXT,
        UNIQUE(institution_id, fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_review_items_open ON intel_review_items(institution_id, status, created_at)",
    """
    CREATE TABLE IF NOT EXISTS intel_incidents (
        incident_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        severity TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        title TEXT NOT NULL,
        signals_json TEXT NOT NULL DEFAULT '[]',
        examples_json TEXT NOT NULL DEFAULT '[]',
        guidance TEXT NOT NULL DEFAULT '',
        connector TEXT NOT NULL DEFAULT '',
        run_id TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        times_seen INTEGER NOT NULL DEFAULT 1,
        notified_at TEXT,
        acknowledged_by TEXT,
        acknowledged_at TEXT,
        resolved_by TEXT,
        resolved_at TEXT,
        note TEXT NOT NULL DEFAULT '',
        UNIQUE(institution_id, fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_incidents_open ON intel_incidents(institution_id, status, severity)",
    # Conditional-request validators, per institution: a 304 answer is only
    # meaningful against this institution's own last reading of the page.
    """
    CREATE TABLE IF NOT EXISTS intel_fetch_validators (
        institution_id TEXT NOT NULL,
        url_sha256 TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        outcome TEXT NOT NULL,
        content_sha256 TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY(institution_id, url_sha256)
    )
    """,
    # The epoch of each institution's ownership tokens: a manager rotates it
    # when a token may be in the wrong hands (it is public by design, so a
    # lapsed or hijacked domain keeps serving it), and every old token stops
    # proving anything.
    """
    CREATE TABLE IF NOT EXISTS intel_owner_tokens (
        institution_id TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL DEFAULT 0,
        rotated_by TEXT NOT NULL DEFAULT '',
        rotated_at TEXT NOT NULL
    )
    """,
    # Global tables: public-web metadata and platform-wide spend, never tenant
    # data, so they stay outside row-level security and a tick can use them
    # for every institution.
    """
    CREATE TABLE IF NOT EXISTS intel_budget_ledger (
        day TEXT NOT NULL,
        connector TEXT NOT NULL,
        units REAL NOT NULL DEFAULT 0,
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day, connector)
    )
    """,
    # Public-web answers (search results, open-API bodies) shared by every
    # institution, keyed by a hash of the request; never page validators.
    """
    CREATE TABLE IF NOT EXISTS intel_shared_cache (
        key_sha256 TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_shared_cache_expiry ON intel_shared_cache(expires_at)",
    # When each host may next be fetched (epoch seconds), shared by every worker
    # process so one site never gets two of them at once.
    """
    CREATE TABLE IF NOT EXISTS intel_host_slots (
        host TEXT PRIMARY KEY,
        next_allowed_at DOUBLE PRECISION NOT NULL
    )
    """,

)
# Columns added after the table first existed; created where missing at start-up.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("intel_assets", "last_activity_at", "TEXT"),
    ("intel_assets", "registration_expires_at", "TEXT"),
    # A running average of what a source yields; productive sources are claimed first.
    ("intel_sources", "priority", "REAL NOT NULL DEFAULT 0"),
    # The interval a source started with; adaptive intervals move around it.
    ("intel_sources", "base_interval_seconds", "INTEGER"),
    # A proposal keeps its own reasons and rule, and the run that made it, so
    # a held run is published or discarded on its own and the published
    # grade's reasons are never overwritten by a proposal.
    ("intel_assets", "proposed_reasons_json", "TEXT"),
    ("intel_assets", "proposed_scorer", "TEXT"),
    ("intel_assets", "proposed_run_id", "TEXT"),
    # The asset a lead was found on (a hub, a site), so rejecting or forgetting it takes the lead too.
    ("intel_sources", "parent_asset_id", "TEXT"),
)
TENANT_TABLES: tuple[str, ...] = ("intel_entities", "intel_assets", "intel_evidence", "intel_gold_items", "intel_suppression", "intel_map_runs", "intel_sources", "intel_quota", "intel_review_items", "intel_incidents", "intel_fetch_validators", "intel_owner_tokens")
GLOBAL_TABLES: tuple[str, ...] = ("intel_budget_ledger", "intel_shared_cache", "intel_host_slots")
SOURCE_CLASSES = frozenset({"rotation", "recheck", "explore"})
REVIEW_KINDS = frozenset({"impersonation_candidate", "court_record", "dispute", "canary_leak", "run_gate", "candidate_account", "news_mention", "lookalike_domain", "authority_nomination", "handle_reassigned"})
REVIEW_STATUSES = frozenset({"open", "decided", "expired"})
# The queue is for people: past this many open items, new ones are refused
# (and counted) rather than burying the ones already waiting.
MAX_OPEN_REVIEW = 500
# Kinds that are never refused for a full queue: a held run, a leaked look-alike,
# a possible impersonator or a court record must reach a person whatever else waits.
URGENT_REVIEW_KINDS = frozenset({"run_gate", "canary_leak", "impersonation_candidate", "court_record"})
REVIEW_TTL_DAYS = 90
SOURCE_ORIGINS = frozenset({"seed", "recurring", "lead", "gap", "profile"})
# Evidence whose detail is free text for people to read. Only these are
# scrubbed of names on the way in: the grader parses the details of the other
# kinds ("A:footer", "authority:nirf", "parked:...", "not_found:api").
FREE_TEXT_EVIDENCE = frozenset({
    "search_snippet", "imported_claim", "reviewer_confirm", "reviewer_reject", "impersonation", "lookalike", "spam_indexed", "community_record", "backlink", "api_identity",
})
# A quota or spend row only counts on its own day, so it goes after a month at
# most, whatever the retention period (see MapStore.prune_retention).
LEDGER_RETENTION_DAYS = 30
RETENTION_COUNTS = ("review_items", "incidents", "map_runs", "quota", "fetch_validators", "sources", "evidence_blanked", "observations")


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def suppression_fingerprint(key: bytes, identifier: str) -> str:
    """A keyed fingerprint of a suppressed identifier; the identifier itself is never stored."""

    return hmac.new(key, identifier.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


def render_sql_migration() -> str:
    lines = [f"-- Agentic Saffron internet map schema {SCHEMA_VERSION}", "-- Generated from app.internet_intelligence.map.store; edit the store, not this file."]
    lines.extend(" ".join(statement.split()) + ";" for statement in _STATEMENTS)
    lines.extend(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type};" for table, column, column_type in ADDED_COLUMNS)
    lines.append("-- PostgreSQL row-level security (skipped on SQLite)")
    lines.extend(statement + ";" for statement in idempotent_tenant_isolation_sql(TENANT_TABLES))
    return "\n".join(lines) + "\n"


class MapStoreScheduling:
    """Sources, leases, budgets and per-institution fetch validators (mixed into MapStore)."""

    backend: SqlBackend

    def _tenant(self, institution_id: str):  # pragma: no cover - provided by MapStore
        raise NotImplementedError

    # ---------------------------------------------------------------- sources
    def upsert_source(
        self, institution_id: str, *, connector: str, target: str, entity_id: str | None = None, asset_id: str | None = None, topic: str = "", origin: str = "seed",
        work_class: str = "rotation", hops: int = 0, interval_seconds: int = 86400, due_at: str | None = None, expires_at: str | None = None, parent_asset_id: str | None = None,
    ) -> tuple[str, bool]:
        """Add a source unless it exists; an existing one keeps its schedule and history."""

        if origin not in SOURCE_ORIGINS or work_class not in SOURCE_CLASSES:
            raise ValueError("unknown source origin or work class")
        target = target.strip()[:1000]
        if not target:
            raise ValueError("a source needs a target")
        stamp = now_iso()
        with self._tenant(institution_id):
            existing = self.backend.fetchone("SELECT source_id, hops FROM intel_sources WHERE institution_id = ? AND connector = ? AND target = ?", (institution_id, connector, target))
            if existing is not None:
                # A shorter path to a known lead lowers its hop count.
                if hops < int(existing["hops"]):
                    self.backend.execute("UPDATE intel_sources SET hops = ?, updated_at = ? WHERE institution_id = ? AND source_id = ?", (hops, stamp, institution_id, existing["source_id"]))
                return str(existing["source_id"]), False
            source_id = f"isrc-{uuid4().hex}"
            self.backend.execute(
                "INSERT INTO intel_sources(source_id, institution_id, connector, target, entity_id, asset_id, parent_asset_id, topic, origin, work_class, hops, due_at, interval_seconds, base_interval_seconds, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, institution_id, connector, target, entity_id, asset_id, parent_asset_id, topic[:100], origin, work_class, max(0, hops), due_at or stamp, max(60, int(interval_seconds)), max(60, int(interval_seconds)), expires_at, stamp, stamp),
            )
            return source_id, True

    def get_source(self, institution_id: str, source_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT * FROM intel_sources WHERE institution_id = ? AND source_id = ?", (institution_id, source_id))

    def list_sources(self, institution_id: str, *, status: str | None = None, connector: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        for column, value in (("status", status), ("connector", connector)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        with self._tenant(institution_id):
            return self.backend.fetchall(f"SELECT * FROM intel_sources WHERE {' AND '.join(clauses)} ORDER BY due_at, source_id LIMIT ?", (*params, max(1, min(limit, 10000))))

    def claim_due(
        self, institution_id: str, *, now: str, worker: str, lease_seconds: int, limit: int, work_class: str | None = None, connectors: Sequence[str] | None = None, order: str = "due",
    ) -> list[dict[str, Any]]:
        """Lease up to ``limit`` due sources; a source leased by another live worker is skipped.

        ``order`` is "due" (longest overdue first) or "priority" (coverage
        gaps, then the most productive, first). The lease is written with a
        condition on the lease columns, so two workers racing for the same
        source cannot both win it.
        """

        lease_until = (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[dict[str, Any]] = []
        with self._tenant(institution_id):
            clauses, params = ["institution_id = ?", "status = 'active'", "due_at <= ?", "(lease_until IS NULL OR lease_until < ?)"], [institution_id, now, now]
            if work_class:
                clauses.append("work_class = ?")
                params.append(work_class)
            if connectors is not None:
                if not connectors:
                    return []
                clauses.append(f"connector IN ({', '.join('?' for _ in connectors)})")
                params.extend(connectors)
            ordering = "CASE WHEN topic = 'gap' THEN 0 ELSE 1 END, priority DESC, due_at, source_id" if order == "priority" else "due_at, source_id"
            candidates = self.backend.fetchall(f"SELECT * FROM intel_sources WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT ?", (*params, max(1, limit * 3)))
            for row in candidates:
                if len(claimed) >= limit:
                    break
                won = self.backend.execute(
                    "UPDATE intel_sources SET lease_owner = ?, lease_until = ? WHERE institution_id = ? AND source_id = ? AND (lease_until IS NULL OR lease_until < ?)",
                    (worker, lease_until, institution_id, row["source_id"], now),
                )
                if won:
                    claimed.append({**row, "lease_owner": worker, "lease_until": lease_until})
        return claimed

    def release_source(self, institution_id: str, source_id: str) -> None:
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_sources SET lease_owner = NULL, lease_until = NULL WHERE institution_id = ? AND source_id = ?", (institution_id, source_id))

    def defer_source(self, institution_id: str, source_id: str, *, due_at: str) -> None:
        """Hand a source back unrun (its host was busy): not a run, not a failure, due again at ``due_at``."""
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_sources SET lease_owner = NULL, lease_until = NULL, due_at = ? WHERE institution_id = ? AND source_id = ?", (due_at, institution_id, source_id))

    def complete_source(
        self, institution_id: str, source_id: str, *, outcome: str, next_due: str, interval_seconds: int, yield_count: int, cost: float, failed: bool,
        etag: str | None = None, last_modified: str | None = None, status: str | None = None, origin: str | None = None, clear_expiry: bool = False, work_class: str | None = None,
    ) -> None:
        with self._tenant(institution_id):
            if clear_expiry:
                self.backend.execute("UPDATE intel_sources SET expires_at = NULL WHERE institution_id = ? AND source_id = ?", (institution_id, source_id))
            self.backend.execute(
                """
                UPDATE intel_sources SET last_outcome = ?, due_at = ?, interval_seconds = ?, yield_last = ?, yield_total = yield_total + ?, cost_total = cost_total + ?, runs = runs + 1,
                    priority = priority * 0.7 + CAST(? AS REAL) * 0.3,
                    failure_streak = CASE WHEN ? = 1 THEN failure_streak + 1 ELSE 0 END, etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),
                    status = COALESCE(?, status), origin = COALESCE(?, origin), work_class = COALESCE(?, work_class), lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE institution_id = ? AND source_id = ?
                """,
                (outcome[:60], next_due, max(60, int(interval_seconds)), yield_count, yield_count, float(cost), float(min(yield_count, 5)), 1 if failed else 0, etag, last_modified, status, origin, work_class, now_iso(), institution_id, source_id),
            )

    def mark_gap_sources(self, institution_id: str, connector: str, gap_targets: Iterable[str], *, now: str, max_interval: int) -> int:
        """Flag the sources that search where the map has no verified account yet, and bring them round sooner.

        A flagged source is also due within ``max_interval`` of ``now``: a
        shorter interval alone would leave one scheduled two weeks out waiting
        the two weeks.
        """

        targets = list(dict.fromkeys(gap_targets))
        stamp = now_iso()
        due_by = (datetime.fromisoformat(now) + timedelta(seconds=int(max_interval))).isoformat()
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_sources SET topic = '', updated_at = ? WHERE institution_id = ? AND connector = ? AND topic = 'gap'", (stamp, institution_id, connector))
            marked = 0
            for chunk in iter_chunks(targets, 200):
                marks = ", ".join("?" for _ in chunk)
                marked += self.backend.execute(
                    f"UPDATE intel_sources SET topic = 'gap', interval_seconds = CASE WHEN interval_seconds > ? THEN ? ELSE interval_seconds END, due_at = CASE WHEN due_at > ? THEN ? ELSE due_at END, updated_at = ? "
                    f"WHERE institution_id = ? AND connector = ? AND status = 'active' AND target IN ({marks})",
                    (int(max_interval), int(max_interval), due_by, due_by, stamp, institution_id, connector, *chunk),
                )
        return marked

    def backfill_base_intervals(self, institution_id: str, defaults: Mapping[str, int]) -> int:
        """Give sources from before ``base_interval_seconds`` existed (NULL after the upgrade) their connector's default as their base.

        Without one, the current interval stood in for the base, so a gap
        search stayed at the gap pace for good and a productive source
        ratcheted down to the hourly minimum.
        """

        with self._tenant(institution_id):
            return sum(
                self.backend.execute("UPDATE intel_sources SET base_interval_seconds = ? WHERE institution_id = ? AND connector = ? AND base_interval_seconds IS NULL", (max(60, int(seconds)), institution_id, connector))
                for connector, seconds in defaults.items()
            )

    def source_yields(self, institution_id: str) -> list[dict[str, Any]]:
        """Per connector: sources, runs, what they found and what they cost."""

        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                "SELECT connector, COUNT(*) AS sources, SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, SUM(runs) AS runs, SUM(yield_total) AS found, SUM(cost_total) AS cost "
                "FROM intel_sources WHERE institution_id = ? GROUP BY connector ORDER BY connector",
                (institution_id,),
            )
        return [{**row, "found_per_run": round(int(row["found"] or 0) / int(row["runs"]), 3) if row["runs"] else None} for row in rows]

    def set_source_status(self, institution_id: str, source_id: str, status: str) -> None:
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_sources SET status = ?, lease_owner = NULL, lease_until = NULL, updated_at = ? WHERE institution_id = ? AND source_id = ?", (status, now_iso(), institution_id, source_id))

    def expire_sources(self, institution_id: str, *, now: str) -> int:
        """Leads past their expiry that ran and never produced anything stop being scheduled (kept for audit).

        A lead that never got to run (deferred by an exhausted budget, or
        while discovery was paused) is not judged yet, so it does not expire.
        """

        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE intel_sources SET status = 'expired', updated_at = ? WHERE institution_id = ? AND status = 'active' AND expires_at IS NOT NULL AND expires_at < ? AND yield_total = 0 AND runs > 0",
                (now, institution_id, now),
            )

    # ---------------------------------------------------------------- budgets
    def reserve_budget(self, institution_id: str, *, connector: str, units: float, day: str, global_cap: float, tenant_cap: float) -> bool:
        """Reserve spend against the tenant's quota and the platform-wide cap, or neither.

        Each conditional upsert only applies while the running total stays
        within its cap, so concurrent workers cannot overspend. The tenant
        quota is taken first and handed back if the global cap refuses.
        """

        if units <= 0:
            return True
        if units > tenant_cap or units > global_cap:
            return False
        with self._tenant(institution_id):
            took_tenant = self.backend.execute(
                "INSERT INTO intel_quota(institution_id, day, connector, units, calls) VALUES (?, ?, ?, ?, 1) ON CONFLICT (institution_id, day, connector) DO UPDATE SET units = intel_quota.units + excluded.units, calls = intel_quota.calls + 1 WHERE intel_quota.units + excluded.units <= ?",
                (institution_id, day, connector, float(units), float(tenant_cap)),
            )
        if not took_tenant:
            return False
        with self.backend.transaction():
            took_global = self.backend.execute(
                "INSERT INTO intel_budget_ledger(day, connector, units, calls) VALUES (?, ?, ?, 1) ON CONFLICT (day, connector) DO UPDATE SET units = intel_budget_ledger.units + excluded.units, calls = intel_budget_ledger.calls + 1 WHERE intel_budget_ledger.units + excluded.units <= ?",
                (day, connector, float(units), float(global_cap)),
            )
        if not took_global:
            with self._tenant(institution_id):
                self.backend.execute("UPDATE intel_quota SET units = units - ?, calls = calls - 1 WHERE institution_id = ? AND day = ? AND connector = ?", (float(units), institution_id, day, connector))
            return False
        return True

    def refund_budget(self, institution_id: str, *, connector: str, units: float, day: str) -> None:
        """Hand back what a run reserved but did not spend (a 304, a cache hit), to the tenant and the platform."""

        if units <= 0:
            return
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_quota SET units = CASE WHEN units > ? THEN units - ? ELSE 0 END WHERE institution_id = ? AND day = ? AND connector = ?", (float(units), float(units), institution_id, day, connector))
        with self.backend.transaction():
            self.backend.execute("UPDATE intel_budget_ledger SET units = CASE WHEN units > ? THEN units - ? ELSE 0 END WHERE day = ? AND connector = ?", (float(units), float(units), day, connector))

    def claim_host_slot(self, host: str, interval: float, *, now: float | None = None) -> float:
        """Claim the next request to ``host`` for this process: 0, or the seconds until it may try again.

        The fetcher's ``host_slots``. Like ``reserve_budget`` the claim is one
        conditional upsert, applied only once the host's next allowed time has
        passed, so of several workers exactly one gets each slot. The fetcher
        asks for the gap plus its whole request deadline, so the claim is a
        lease that holds the host while the request runs; ``release_host_slot``
        hands it back when the request is over.
        """

        moment = time.time() if now is None else now
        key = host.strip().lower().rstrip(".")[:255]
        with self.backend.transaction():
            took = self.backend.execute(
                "INSERT INTO intel_host_slots(host, next_allowed_at) VALUES (?, ?) ON CONFLICT (host) DO UPDATE SET next_allowed_at = excluded.next_allowed_at WHERE intel_host_slots.next_allowed_at <= ?",
                (key, moment + float(interval), moment),
            )
            row = None if took else self.backend.fetchone("SELECT next_allowed_at FROM intel_host_slots WHERE host = ?", (key,))
        if took:
            return 0.0
        return max(0.0, float(row["next_allowed_at"]) - moment) if row else float(interval)

    def release_host_slot(self, host: str, interval: float, *, now: float | None = None) -> None:
        """Hand back a host this process claimed, its request now over: the next may start ``interval`` from now.

        The fetcher's ``host_release``, called when a request ends however it
        ended. The claim's lease outlasts the request (the request is cut off
        at the fetcher's deadline, the lease runs a gap past it), so the row
        still holds this worker's lease and no one else's.
        """

        moment = time.time() if now is None else now
        with self.backend.transaction():
            self.backend.execute("UPDATE intel_host_slots SET next_allowed_at = ? WHERE host = ?", (moment + float(interval), host.strip().lower().rstrip(".")[:255]))

    def spend(self, *, day: str) -> dict[str, dict[str, float]]:
        with self.backend.transaction():
            rows = self.backend.fetchall("SELECT connector, units, calls FROM intel_budget_ledger WHERE day = ? ORDER BY connector", (day,))
        return {str(row["connector"]): {"units": float(row["units"]), "calls": int(row["calls"])} for row in rows}

    def tenant_spend(self, institution_id: str, *, day: str) -> dict[str, dict[str, float]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT connector, units, calls FROM intel_quota WHERE institution_id = ? AND day = ? ORDER BY connector", (institution_id, day))
        return {str(row["connector"]): {"units": float(row["units"]), "calls": int(row["calls"])} for row in rows}

    # -------------------------------------------------------------- fetch cache
    def fetch_state(self, institution_id: str, url: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT * FROM intel_fetch_validators WHERE institution_id = ? AND url_sha256 = ?", (institution_id, hashlib.sha256(url.encode("utf-8")).hexdigest()))

    def record_fetch(self, institution_id: str, url: str, *, outcome: str, etag: str | None, last_modified: str | None, content_sha256: str | None) -> None:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO intel_fetch_validators(institution_id, url_sha256, etag, last_modified, outcome, content_sha256, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (institution_id, url_sha256) DO UPDATE SET etag = excluded.etag, last_modified = excluded.last_modified, outcome = excluded.outcome, "
                "content_sha256 = COALESCE(excluded.content_sha256, intel_fetch_validators.content_sha256), fetched_at = excluded.fetched_at",
                (institution_id, key, etag, last_modified, outcome[:40], content_sha256, now_iso()),
            )

    # ------------------------------------------------------------ shared cache
    @staticmethod
    def _shared_key(kind: str, key: str) -> str:
        """Only a hash of the request is stored, so a query or URL never sits in the table in clear."""

        return hashlib.sha256(f"{kind}\n{key}".encode("utf-8")).hexdigest()

    def cache_get(self, kind: str, key: str, *, now: str | None = None) -> Any | None:
        """A public-web answer any institution fetched before, or None when there is none or it has expired."""

        with self.backend.transaction():
            row = self.backend.fetchone("SELECT payload FROM intel_shared_cache WHERE key_sha256 = ? AND kind = ? AND expires_at > ?", (self._shared_key(kind, key), kind, now or now_iso()))
        return _loads(row["payload"], None) if row else None

    def cache_put(self, kind: str, key: str, payload: Any, ttl_seconds: int, *, now: str | None = None) -> None:
        stamp = now or now_iso()
        expires = (datetime.fromisoformat(stamp) + timedelta(seconds=max(0, int(ttl_seconds)))).isoformat()
        with self.backend.transaction():
            self.backend.execute(
                "INSERT INTO intel_shared_cache(key_sha256, kind, payload, fetched_at, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (key_sha256) DO UPDATE SET kind = excluded.kind, payload = excluded.payload, fetched_at = excluded.fetched_at, expires_at = excluded.expires_at",
                (self._shared_key(kind, key), kind, _json(payload), stamp, expires),
            )

    def cache_prune(self, now: str | None = None) -> int:
        """Drop expired answers (the daily digest job calls this)."""

        with self.backend.transaction():
            return self.backend.execute("DELETE FROM intel_shared_cache WHERE expires_at <= ?", (now or now_iso(),))

    # ---------------------------------------------------------------- run lock
    def try_start_map_run(self, institution_id: str, *, kind: str, lock_seconds: int) -> str | None:
        """Start a run of ``kind`` unless one is in progress; a run silent past ``lock_seconds`` is abandoned."""

        run_id = f"imrn-{uuid4().hex}"
        started = datetime.now(timezone.utc)
        cutoff = (started - timedelta(seconds=lock_seconds)).isoformat()
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE intel_map_runs SET status = 'abandoned', finished_at = ?, error = 'the run lost its process' WHERE institution_id = ? AND kind = ? AND status = 'running' AND started_at < ?",
                (started.isoformat(), institution_id, kind, cutoff),
            )
            inserted = self.backend.execute(
                "INSERT INTO intel_map_runs(run_id, institution_id, kind, started_at, status) SELECT CAST(? AS TEXT), CAST(? AS TEXT), CAST(? AS TEXT), CAST(? AS TEXT), 'running' WHERE NOT EXISTS (SELECT 1 FROM intel_map_runs WHERE institution_id = ? AND kind = ? AND status = 'running') "
                "ON CONFLICT (institution_id, kind) WHERE status = 'running' AND kind = 'tick' DO NOTHING",
                (run_id, institution_id, kind, started.isoformat(), institution_id, kind),
            )
        return run_id if inserted else None


class MapStoreReview:
    """The review queue: things a person decides before the map concludes anything (mixed into MapStore)."""

    backend: SqlBackend

    def _tenant(self, institution_id: str):  # pragma: no cover - provided by MapStore
        raise NotImplementedError

    def _without_names(self, institution_id: str, text: str) -> str:
        """The backstop for text a connector forgot to scrub: people's names out, the institution's entities' names kept.

        Most text has no name-shaped phrase at all, so the entity names are
        read only when a first pass would change something.
        """

        if not text or strip_person_names(text) == text:
            return text
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT name, names_json FROM intel_entities WHERE institution_id = ?", (institution_id,))
        return strip_person_names(text, keep=names_of({"name": row["name"], "names": _loads(row["names_json"], [])} for row in rows))

    def add_review_item(
        self, institution_id: str, *, kind: str, title: str, fingerprint: str | None = None, detail: str = "", url: str = "", asset_id: str | None = None, entity_id: str | None = None,
        severity: str = "normal", connector: str = "", source_id: str | None = None, run_id: str | None = None, now: str | None = None,
    ) -> tuple[str | None, str]:
        """Queue an item once; returns (review_id, 'added' | 'exists' | 'reopened' | 'full').

        The fingerprint makes it idempotent: the same court record or the same
        suspected impersonator found again is the same item, open or decided,
        so a decision is never asked for twice. It names the asset when there
        is one (a search hit spells the same account's URL many ways), else
        the URL as ``asset_ref`` normalises it. An item that expired
        undecided was never answered, so finding it again reopens it.
        """

        if kind not in REVIEW_KINDS:
            raise ValueError(f"unknown review kind {kind!r}")
        if severity not in {"low", "normal", "high"}:
            raise ValueError("severity must be low, normal or high")
        stamp = now or now_iso()
        try:
            spelled = asset_ref(url).url if url.strip() else ""
        except ValueError:
            spelled = url.strip()
        key = fingerprint or hashlib.sha256((f"{kind}|{asset_id}" if asset_id else f"{kind}||{spelled}|{'' if url else title}").encode("utf-8")).hexdigest()
        # Items queued before the fingerprint named the asset are still found under their old one.
        legacy = fingerprint or hashlib.sha256(f"{kind}|{asset_id or ''}|{url}|{'' if asset_id or url else title}".encode("utf-8")).hexdigest()
        expires = (datetime.fromisoformat(stamp) + timedelta(days=REVIEW_TTL_DAYS)).isoformat()
        # The fingerprint above stays on the title as found, so items queued before names were scrubbed still match.
        title, detail = self._without_names(institution_id, title), self._without_names(institution_id, detail)
        with self._tenant(institution_id):
            existing = self.backend.fetchone(
                "SELECT review_id, status FROM intel_review_items WHERE institution_id = ? AND fingerprint IN (?, ?) ORDER BY CASE WHEN fingerprint = ? THEN 0 ELSE 1 END LIMIT 1", (institution_id, key, legacy, key),
            )
            if existing is not None and existing["status"] != "expired":
                return str(existing["review_id"]), "exists"
            waiting = self.backend.fetchone("SELECT COUNT(*) AS open_items FROM intel_review_items WHERE institution_id = ? AND status = 'open'", (institution_id,))
            if severity != "high" and kind not in URGENT_REVIEW_KINDS and waiting and int(waiting["open_items"]) >= MAX_OPEN_REVIEW:
                return None, "full"
            if existing is not None:
                self.backend.execute(
                    "UPDATE intel_review_items SET status = 'open', title = ?, detail = ?, run_id = COALESCE(?, run_id), expires_at = ?, updated_at = ? WHERE institution_id = ? AND review_id = ? AND status = 'expired'",
                    (title[:300], detail[:2000], run_id, expires, stamp, institution_id, existing["review_id"]),
                )
                return str(existing["review_id"]), "reopened"
            review_id = f"irev-{uuid4().hex}"
            self.backend.execute(
                "INSERT INTO intel_review_items(review_id, institution_id, kind, fingerprint, severity, asset_id, entity_id, title, detail, url, connector, source_id, run_id, created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, institution_id, kind, key, severity, asset_id, entity_id, title[:300], detail[:2000], url[:1000], connector[:40], source_id, run_id, stamp, stamp, expires),
            )
            return review_id, "added"

    def get_review_item(self, institution_id: str, review_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT * FROM intel_review_items WHERE institution_id = ? AND review_id = ?", (institution_id, review_id))

    def list_review_items(self, institution_id: str, *, status: str | None = "open", kind: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        for column, value in (("status", status), ("kind", kind)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        with self._tenant(institution_id):
            return self.backend.fetchall(
                f"SELECT * FROM intel_review_items WHERE {' AND '.join(clauses)} ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 500)), max(0, offset)),
            )

    def review_counts(self, institution_id: str) -> dict[str, int]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT kind, COUNT(*) AS items FROM intel_review_items WHERE institution_id = ? AND status = 'open' GROUP BY kind", (institution_id,))
        return {str(row["kind"]): int(row["items"]) for row in rows}

    def decide_review_item(self, institution_id: str, review_id: str, *, decision: str, decided_by: str, note: str = "", redact: bool = False) -> bool:
        """Close an open item with a decision; returns False when it was not open (decided twice, expired)."""

        stamp = now_iso()
        with self._tenant(institution_id):
            if redact:
                # A personal account leaves no trace of who it was, here either.
                return bool(self.backend.execute(
                    "UPDATE intel_review_items SET status = 'decided', decision = ?, decision_note = ?, decided_by = ?, decided_at = ?, updated_at = ?, title = 'a personal account (removed from the map)', detail = '', url = '', asset_id = NULL "
                    "WHERE institution_id = ? AND review_id = ? AND status = 'open'",
                    (decision, note[:500], decided_by, stamp, stamp, institution_id, review_id),
                ))
            return bool(self.backend.execute(
                "UPDATE intel_review_items SET status = 'decided', decision = ?, decision_note = ?, decided_by = ?, decided_at = ?, updated_at = ? WHERE institution_id = ? AND review_id = ? AND status = 'open'",
                (decision, note[:500], decided_by, stamp, stamp, institution_id, review_id),
            ))

    def expire_review_items(self, institution_id: str, *, now: str) -> int:
        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE intel_review_items SET status = 'expired', updated_at = ? WHERE institution_id = ? AND status = 'open' AND expires_at IS NOT NULL AND expires_at < ?", (now, institution_id, now),
            )

    # ------------------------------------------------ decisions on assets
    def set_relation(self, institution_id: str, asset_id: str, relation: str) -> None:
        if relation not in RELATIONS:
            raise ValueError(f"unknown relation {relation!r}")
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_assets SET relation = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?", (relation, now_iso(), institution_id, asset_id))

    def _sources_from(self, institution_id: str, asset_id: str, *, max_hops: int) -> list[str]:
        """The sources found on an asset, and on what those led to, up to ``max_hops`` steps (each lead names its parent)."""

        found: dict[str, None] = {}
        parents, seen = [asset_id], {asset_id}
        for _ in range(max(0, max_hops)):
            rows = [
                row for chunk in iter_chunks(parents, 200)
                for row in self.backend.fetchall(f"SELECT source_id, asset_id FROM intel_sources WHERE institution_id = ? AND parent_asset_id IN ({', '.join('?' for _ in chunk)})", (institution_id, *chunk))
            ]
            found.update((str(row["source_id"]), None) for row in rows)
            parents = list(dict.fromkeys(str(row["asset_id"]) for row in rows if row["asset_id"] and row["asset_id"] not in seen))
            seen.update(parents)
            if not parents:
                break
        return list(found)

    def prune_sources_for(self, institution_id: str, *, asset_id: str, url: str, whole_host: bool = False, max_hops: int = 2) -> int:
        """Stop following a rejected asset, the leads found on it, and (for a rejected website) the leads on its host.

        A social account shares its host with every other account on that
        platform, so only the account's own sources are pruned for it. Leads
        name a host as they found it, so every spelling (www. or not, http or
        https) of a rejected site's host is pruned.
        """

        stamp = now_iso()
        with self._tenant(institution_id):
            pruned = self.backend.execute("UPDATE intel_sources SET status = 'pruned', updated_at = ? WHERE institution_id = ? AND status = 'active' AND (asset_id = ? OR target = ? OR target = ?)", (stamp, institution_id, asset_id, asset_id, url))
            for chunk in iter_chunks(self._sources_from(institution_id, asset_id, max_hops=max_hops), 200):
                pruned += self.backend.execute(f"UPDATE intel_sources SET status = 'pruned', updated_at = ? WHERE institution_id = ? AND status = 'active' AND source_id IN ({', '.join('?' for _ in chunk)})", (stamp, institution_id, *chunk))
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
            if whole_host and host:
                bases = [f"{scheme}://{name}" for scheme in ("https", "http") for name in (host, f"www.{host}")]
                pruned += self.backend.execute(
                    f"UPDATE intel_sources SET status = 'pruned', updated_at = ? WHERE institution_id = ? AND status = 'active' AND ({' OR '.join('target = ? OR target LIKE ?' for _ in bases)})",
                    (stamp, institution_id, *[value for base in bases for value in (base, f"{base}/%")]),
                )
        return pruned

    def cited_by(self, institution_id: str, asset_id: str) -> list[str]:
        """The other assets holding evidence this one gave them (as its source), which ``forget_asset`` deletes with it."""

        with self._tenant(institution_id):
            asset = self.backend.fetchone("SELECT url FROM intel_assets WHERE institution_id = ? AND asset_id = ?", (institution_id, asset_id))
            if asset is None:
                return []
            rows = self.backend.fetchall(
                "SELECT DISTINCT asset_id FROM intel_evidence WHERE institution_id = ? AND asset_id <> ? AND (source_asset_id = ? OR source_url = ?)", (institution_id, asset_id, asset_id, asset["url"]),
            )
        return sorted(str(row["asset_id"]) for row in rows)

    def forget_asset(self, institution_id: str, asset_id: str, *, keep_review_id: str | None = None) -> bool:
        """Remove an asset, its evidence and its sources entirely.

        The evidence log is otherwise append-only; this is the one exception,
        for an account that turned out to be a person's: the map keeps only a
        keyed fingerprint (the suppression list) so it is never added again.
        Every review item and incident that named it is redacted too
        (``keep_review_id`` is left for the caller that is deciding it). So
        is what it left elsewhere: the evidence it gave other assets (a hub
        link names the hub in its source URL and channel; the caller regrades
        them, ``cited_by`` lists them first), the leads found on it, its
        conditional-fetch validators (a hash of a guessable URL still names
        it) and any other asset's note that mentions its handle.
        """

        from .connectors.feeds import youtube_feed_url  # a module-level import would be circular

        stamp = now_iso()
        with self._tenant(institution_id):
            asset = self.backend.fetchone("SELECT asset_key, url, handle FROM intel_assets WHERE institution_id = ? AND asset_id = ?", (institution_id, asset_id))
            if asset is None:
                return False
            for chunk in iter_chunks(self._sources_from(institution_id, asset_id, max_hops=2), 200):
                self.backend.execute(f"DELETE FROM intel_sources WHERE institution_id = ? AND source_id IN ({', '.join('?' for _ in chunk)})", (institution_id, *chunk))
            self.backend.execute("DELETE FROM intel_evidence WHERE institution_id = ? AND (source_asset_id = ? OR source_url = ?)", (institution_id, asset_id, asset["url"]))
            fetched = [asset["url"], *([youtube_feed_url(asset["asset_key"].removeprefix("youtube:channel:"))] if asset["asset_key"].startswith("youtube:channel:") else [])]
            self.backend.execute(
                f"DELETE FROM intel_fetch_validators WHERE institution_id = ? AND url_sha256 IN ({', '.join('?' for _ in fetched)})", (institution_id, *[hashlib.sha256(url.encode("utf-8")).hexdigest() for url in fetched]),
            )
            handle = str(asset["handle"] or "").lstrip("@")
            if len(handle) >= 3:
                pattern = "%" + handle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                self.backend.execute("UPDATE intel_assets SET note = '', updated_at = ? WHERE institution_id = ? AND asset_id <> ? AND note LIKE ? ESCAPE '\\'", (stamp, institution_id, asset_id, pattern))
            self.backend.execute(
                "UPDATE intel_review_items SET title = 'a personal account (removed from the map)', detail = '', url = '', asset_id = NULL, updated_at = ?, "
                "status = CASE WHEN status = 'open' THEN 'decided' ELSE status END, decision = CASE WHEN status = 'open' THEN 'personal' ELSE decision END, "
                "decided_by = CASE WHEN status = 'open' THEN 'system:suppression' ELSE decided_by END, decided_at = CASE WHEN status = 'open' THEN ? ELSE decided_at END "
                "WHERE institution_id = ? AND (asset_id = ? OR url = ?) AND review_id <> ?",
                (stamp, stamp, institution_id, asset_id, asset["url"], keep_review_id or ""),
            )
            self.backend.execute("DELETE FROM intel_incidents WHERE institution_id = ? AND target = ?", (institution_id, asset["asset_key"]))
            self.backend.execute("DELETE FROM intel_evidence WHERE institution_id = ? AND asset_id = ?", (institution_id, asset_id))
            self.backend.execute("DELETE FROM intel_sources WHERE institution_id = ? AND (asset_id = ? OR target = ? OR target = ?)", (institution_id, asset_id, asset_id, asset["url"]))
            self.backend.execute("DELETE FROM intel_gold_items WHERE institution_id = ? AND asset_key = ?", (institution_id, asset["asset_key"]))
            self.backend.execute("DELETE FROM intel_assets WHERE institution_id = ? AND asset_id = ?", (institution_id, asset_id))
        return True

    def apply_proposed(self, institution_id: str, *, run_id: str | None = None) -> int:
        """Publish proposed grades (a gate passed or a manager approved them): one run's, or every one."""

        scope, params = ("AND proposed_run_id = ?", (run_id,)) if run_id else ("", ())
        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE intel_assets SET grade = proposed_grade, grade_reasons_json = COALESCE(proposed_reasons_json, grade_reasons_json), scorer_version = COALESCE(proposed_scorer, scorer_version), "
                f"proposed_grade = NULL, proposed_reasons_json = NULL, proposed_scorer = NULL, proposed_run_id = NULL, updated_at = ? WHERE institution_id = ? AND proposed_grade IS NOT NULL {scope}",
                (now_iso(), institution_id, *params),
            )

    def discard_proposed(self, institution_id: str, *, run_id: str | None = None) -> int:
        """Drop proposed grades (one run's, or every one): the published grade stays.

        A manager who discards a re-scoring keeps the grade under the new rule,
        so the same proposal is not raised again at the next pass.
        """

        scope, params = ("AND proposed_run_id = ?", (run_id,)) if run_id else ("", ())
        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE intel_assets SET scorer_version = COALESCE(proposed_scorer, scorer_version), proposed_grade = NULL, proposed_reasons_json = NULL, proposed_scorer = NULL, proposed_run_id = NULL, "
                f"updated_at = ? WHERE institution_id = ? AND proposed_grade IS NOT NULL {scope}",
                (now_iso(), institution_id, *params),
            )

    def hold_changes(self, institution_id: str, held: Sequence[Mapping[str, Any]], *, run_id: str) -> int:
        """Set grades a held run changed back to what readers saw, and park the run's grades as its proposal.

        Each item carries asset_id, the published grade/reasons/scorer before
        the run, and the grade/reasons/scorer the run computed.
        """

        with self._tenant(institution_id):
            for item in held:
                self.backend.execute(
                    "UPDATE intel_assets SET grade = ?, grade_reasons_json = ?, scorer_version = ?, proposed_grade = ?, proposed_reasons_json = ?, proposed_scorer = ?, proposed_run_id = ?, updated_at = ? "
                    "WHERE institution_id = ? AND asset_id = ?",
                    (item["before_grade"], _json(list(item.get("before_reasons") or [])), item.get("before_scorer"), item["grade"], _json(list(item.get("reasons") or [])), item.get("scorer"), run_id, now_iso(), institution_id, item["asset_id"]),
                )
        return len(held)

    def has_stale_scores(self, institution_id: str, scorer_version: str) -> bool:
        """Whether any grade was computed by an older rule (a re-scoring must go through the gate first)."""

        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT 1 AS present FROM intel_assets WHERE institution_id = ? AND scorer_version IS NOT NULL AND scorer_version <> ? LIMIT 1", (institution_id, scorer_version)) is not None


class MapStoreIncidents:
    """Incidents: things that went wrong with the institution's own presence (mixed into MapStore)."""

    backend: SqlBackend

    def _tenant(self, institution_id: str):  # pragma: no cover - provided by MapStore
        raise NotImplementedError

    def upsert_incident(
        self, institution_id: str, *, kind: str, target: str, severity: str, title: str, signals: Sequence[str] = (), examples: Sequence[str] = (), guidance: str = "",
        connector: str = "", run_id: str | None = None, now: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Record an incident once per (kind, target); returns (row, 'new' | 'repeat' | 'reopened' | 'escalated').

        Seeing it again only moves last_seen_at and counts it; a resolved
        incident that comes back is reopened (and will be notified again).
        One seen again at a higher severity (a domain expiring within days,
        not weeks) is escalated: it is open again and not yet notified, so
        the urgent alert goes out even though the digest already named it.
        """

        if severity not in {"low", "medium", "high"}:
            raise ValueError("severity must be low, medium or high")
        stamp = now or now_iso()
        fingerprint = hashlib.sha256(f"{kind}|{target.lower()}".encode("utf-8")).hexdigest()
        with self._tenant(institution_id):
            existing = self.backend.fetchone("SELECT * FROM intel_incidents WHERE institution_id = ? AND fingerprint = ?", (institution_id, fingerprint))
            if existing is None:
                incident_id = f"iinc-{uuid4().hex}"
                self.backend.execute(
                    "INSERT INTO intel_incidents(incident_id, institution_id, kind, target, fingerprint, severity, title, signals_json, examples_json, guidance, connector, run_id, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (incident_id, institution_id, kind[:60], target[:300], fingerprint, severity, title[:300], _json(list(signals)[:20]), _json(list(examples)[:10]), guidance[:2000], connector[:40], run_id, stamp, stamp),
                )
                verdict = "new"
            else:
                incident_id = str(existing["incident_id"])
                reopened = existing["status"] == "resolved"
                ranks = {"low": 0, "medium": 1, "high": 2}
                rises = ranks[severity] > ranks.get(str(existing["severity"]), 0)
                self.backend.execute(
                    "UPDATE intel_incidents SET last_seen_at = ?, times_seen = times_seen + 1, signals_json = ?, examples_json = ?, run_id = COALESCE(?, run_id), "
                    "severity = ?, status = CASE WHEN status = 'resolved' OR ? = 1 THEN 'open' ELSE status END, "
                    "notified_at = CASE WHEN status = 'resolved' OR ? = 1 THEN NULL ELSE notified_at END WHERE institution_id = ? AND incident_id = ?",
                    (stamp, _json(list(signals)[:20]), _json(list(examples)[:10]), run_id, severity if rises else str(existing["severity"]), int(rises), int(rises), institution_id, incident_id),
                )
                verdict = "reopened" if reopened else "escalated" if rises else "repeat"
            row = self.backend.fetchone("SELECT * FROM intel_incidents WHERE institution_id = ? AND incident_id = ?", (institution_id, incident_id))
        return self._incident_row(row), verdict

    @staticmethod
    def _incident_row(row: dict[str, Any]) -> dict[str, Any]:
        row["signals"] = _loads(row.pop("signals_json", "[]"), [])
        row["examples"] = _loads(row.pop("examples_json", "[]"), [])
        return row

    def get_incident(self, institution_id: str, incident_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM intel_incidents WHERE institution_id = ? AND incident_id = ?", (institution_id, incident_id))
        return self._incident_row(row) if row else None

    def list_incidents(self, institution_id: str, *, status: str | None = None, severity: str | None = None, since: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        for column, value in (("status", status), ("severity", severity)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("last_seen_at >= ?")
            params.append(since)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM intel_incidents WHERE {' AND '.join(clauses)} ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, last_seen_at DESC LIMIT ?",
                (*params, max(1, min(limit, 500))),
            )
        return [self._incident_row(row) for row in rows]

    def set_incident_status(self, institution_id: str, incident_id: str, *, status: str, by: str, note: str = "") -> bool:
        if status not in {"acknowledged", "resolved"}:
            raise ValueError("an incident can be acknowledged or resolved")
        stamp = now_iso()
        column = "acknowledged" if status == "acknowledged" else "resolved"
        with self._tenant(institution_id):
            return bool(self.backend.execute(
                f"UPDATE intel_incidents SET status = ?, {column}_by = ?, {column}_at = ?, note = CASE WHEN ? = '' THEN note ELSE ? END WHERE institution_id = ? AND incident_id = ? AND status <> 'resolved'",
                (status, by, stamp, note[:500], note[:500], institution_id, incident_id),
            ))

    def mark_incidents_notified(self, institution_id: str, incident_ids: Sequence[str]) -> None:
        if not incident_ids:
            return
        stamp = now_iso()
        with self._tenant(institution_id):
            for chunk in iter_chunks(list(incident_ids), 200):
                self.backend.execute(f"UPDATE intel_incidents SET notified_at = ? WHERE institution_id = ? AND incident_id IN ({', '.join('?' for _ in chunk)})", (stamp, institution_id, *chunk))


class MapStore(MapStoreScheduling, MapStoreReview, MapStoreIncidents):
    def __init__(self, database_url: str | None = None, backend: SqlBackend | None = None, *, suppression_key: bytes = b"guru-ji-development-only") -> None:
        self.backend = backend or open_backend(database_url)
        self.suppression_key = suppression_key
        self._migrate()

    def _migrate(self) -> None:
        with self.backend.transaction():
            begin_migration(self.backend)
            apply_schema(self.backend, _STATEMENTS)
            add_missing_columns(self.backend, ADDED_COLUMNS)
            if self.backend.dialect == "postgresql":
                for statement in tenant_isolation_statements(TENANT_TABLES, state=row_level_security_state(self.backend), policies=existing_policies(self.backend)):
                    self.backend.execute(statement)
            self.backend.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT (version) DO NOTHING", (SCHEMA_VERSION, now_iso()))

    def close(self) -> None:
        self.backend.close()

    def _tenant(self, institution_id: str):
        if not institution_id.strip():
            raise ValueError("institution_id is required")
        return self.backend.transaction(tenant_id=institution_id)

    # ---------------------------------------------------------------- entities
    def upsert_entity(
        self, institution_id: str, *, name: str, kind: str = "institution", group_label: str = "", names: Sequence[str] = (), locations: Sequence[str] = (),
        parent_id: str | None = None, authority: str = "self", status: str = "active", notes: str = "",
    ) -> str:
        """Add an entity or merge into the existing one.

        ``authority`` only ever moves an existing entity away from 'self' (a
        re-import that now knows the Math is not ours); nothing an import says
        hands another authority's entity back to the institution.
        """

        name = " ".join(name.split())
        if not name:
            raise ValueError("an entity needs a name")
        if kind not in ENTITY_KINDS:
            raise ValueError(f"unknown entity kind {kind!r}")
        stamp = now_iso()
        cleaned_names = list(dict.fromkeys(" ".join(item.split()) for item in names if item and item.strip()))
        with self._tenant(institution_id):
            existing = self.backend.fetchone("SELECT entity_id, names_json, locations_json FROM intel_entities WHERE institution_id = ? AND kind = ? AND name = ?", (institution_id, kind, name))
            if existing is not None:
                merged_names = list(dict.fromkeys([*_loads(existing["names_json"], []), *cleaned_names]))
                merged_locations = list(dict.fromkeys([*_loads(existing["locations_json"], []), *locations]))
                self.backend.execute(
                    "UPDATE intel_entities SET names_json = ?, locations_json = ?, group_label = CASE WHEN ? = '' THEN group_label ELSE ? END, parent_id = COALESCE(?, parent_id), "
                    "authority = CASE WHEN authority = 'self' THEN ? ELSE authority END, updated_at = ? WHERE institution_id = ? AND entity_id = ?",
                    (_json(merged_names), _json(merged_locations), group_label, group_label, parent_id, authority, stamp, institution_id, existing["entity_id"]),
                )
                return str(existing["entity_id"])
            entity_id = f"ient-{uuid4().hex}"
            self.backend.execute(
                "INSERT INTO intel_entities(entity_id, institution_id, kind, name, names_json, locations_json, group_label, parent_id, authority, status, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_id, institution_id, kind, name, _json(cleaned_names), _json(list(dict.fromkeys(locations))), group_label, parent_id, authority, status, notes[:1000], stamp, stamp),
            )
            return entity_id

    def _entity_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["names"] = _loads(row.pop("names_json", "[]"), [])
        row["locations"] = _loads(row.pop("locations_json", "[]"), [])
        return row

    def get_entity(self, institution_id: str, entity_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM intel_entities WHERE institution_id = ? AND entity_id = ?", (institution_id, entity_id))
        return self._entity_row(row) if row else None

    def list_entities(self, institution_id: str, *, kind: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM intel_entities WHERE {' AND '.join(clauses)} ORDER BY group_label, name LIMIT ?", (*params, max(1, min(limit, 5000))))
        return [self._entity_row(row) for row in rows]

    # ------------------------------------------------------------------ assets
    def upsert_asset(self, institution_id: str, ref: AssetRef, *, entity_id: str | None, relation: str = "unknown", note: str = "") -> tuple[str, bool]:
        """Insert or re-link an asset; returns (asset_id, created).

        An existing asset keeps its grade and status (those only change from
        evidence); a relation other than 'unknown' fills in an unknown one.
        """

        if relation not in RELATIONS:
            raise ValueError(f"unknown relation {relation!r}")
        stamp = now_iso()
        with self._tenant(institution_id):
            existing = self.backend.fetchone("SELECT asset_id, relation, entity_id FROM intel_assets WHERE institution_id = ? AND asset_key = ?", (institution_id, ref.key))
            if existing is not None:
                self.backend.execute(
                    "UPDATE intel_assets SET entity_id = COALESCE(entity_id, ?), relation = CASE WHEN relation = 'unknown' THEN ? ELSE relation END, note = CASE WHEN note = '' THEN ? ELSE note END, updated_at = ? WHERE institution_id = ? AND asset_id = ?",
                    (entity_id, relation, note[:500], stamp, institution_id, existing["asset_id"]),
                )
                return str(existing["asset_id"]), False
            asset_id = f"iast-{uuid4().hex}"
            self.backend.execute(
                "INSERT INTO intel_assets(asset_id, institution_id, entity_id, asset_key, platform, kind, url, handle, relation, note, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, institution_id, entity_id, ref.key, ref.platform, ref.kind, ref.url, ref.handle[:200], relation, note[:500], stamp, stamp),
            )
            return asset_id, True

    def _asset_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["grade_reasons"] = _loads(row.pop("grade_reasons_json", "[]"), [])
        row["proposed_reasons"] = _loads(row.pop("proposed_reasons_json", None) or "[]", [])
        return row

    def get_asset(self, institution_id: str, asset_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM intel_assets WHERE institution_id = ? AND asset_id = ?", (institution_id, asset_id))
        return self._asset_row(row) if row else None

    def find_asset(self, institution_id: str, asset_key: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM intel_assets WHERE institution_id = ? AND asset_key = ?", (institution_id, asset_key))
        return self._asset_row(row) if row else None

    def list_assets(
        self, institution_id: str, *, platform: str | None = None, grade: str | None = None, relation: str | None = None, entity_id: str | None = None, status: str | None = None,
        kind: str | None = None, limit: int = 500, offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        for column, value in (("platform", platform), ("grade", grade), ("relation", relation), ("entity_id", entity_id), ("status", status), ("kind", kind)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM intel_assets WHERE {' AND '.join(clauses)} ORDER BY platform, asset_key LIMIT ? OFFSET ?", (*params, max(1, min(limit, 5000)), max(0, offset)),
            )
        return [self._asset_row(row) for row in rows]

    def iter_assets(self, institution_id: str, *, page: int = 1000, **filters: Any) -> Iterator[dict[str, Any]]:
        """Every asset matching the filters, a page at a time (``list_assets`` stops at 5000)."""

        offset = 0
        while True:
            rows = self.list_assets(institution_id, limit=page, offset=offset, **filters)
            yield from rows
            if len(rows) < page:
                return
            offset += page

    def get_assets(self, institution_id: str, asset_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = list(dict.fromkeys(asset_ids))
        found: dict[str, dict[str, Any]] = {}
        with self._tenant(institution_id):
            for chunk in iter_chunks(wanted, 200):
                marks = ", ".join("?" for _ in chunk)
                for row in self.backend.fetchall(f"SELECT * FROM intel_assets WHERE institution_id = ? AND asset_id IN ({marks})", (institution_id, *chunk)):
                    found[str(row["asset_id"])] = self._asset_row(row)
        return found

    def source_targets(self, institution_id: str, connector: str) -> set[str]:
        with self._tenant(institution_id):
            return {str(row["target"]) for row in self.backend.fetchall("SELECT target FROM intel_sources WHERE institution_id = ? AND connector = ?", (institution_id, connector))}

    def set_grade(self, institution_id: str, asset_id: str, *, grade: str, reasons: Sequence[str], scorer_version: str, proposed: bool = False, run_id: str | None = None) -> None:
        """Store a computed grade; ``proposed`` parks it (with its reasons, rule and run) until a gate lets it through.

        A proposal never touches the published grade or its reasons, so
        readers keep seeing exactly what was last published.
        """

        if grade not in GRADE_RANK:
            raise ValueError(f"unknown grade {grade!r}")
        with self._tenant(institution_id):
            if proposed:
                self.backend.execute(
                    "UPDATE intel_assets SET proposed_grade = ?, proposed_reasons_json = ?, proposed_scorer = ?, proposed_run_id = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?",
                    (grade, _json(list(reasons)), scorer_version, run_id, now_iso(), institution_id, asset_id),
                )
            else:
                self.backend.execute(
                    "UPDATE intel_assets SET grade = ?, proposed_grade = NULL, proposed_reasons_json = NULL, proposed_scorer = NULL, proposed_run_id = NULL, grade_reasons_json = ?, scorer_version = ?, updated_at = ? "
                    "WHERE institution_id = ? AND asset_id = ?",
                    (grade, _json(list(reasons)), scorer_version, now_iso(), institution_id, asset_id),
                )

    def restamp_grade(self, institution_id: str, asset_id: str, *, reasons: Sequence[str], scorer_version: str) -> None:
        """Bring an unchanged grade's reasons and rule up to date, leaving any waiting proposal as it is."""

        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE intel_assets SET grade_reasons_json = ?, scorer_version = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?",
                (_json(list(reasons)), scorer_version, now_iso(), institution_id, asset_id),
            )

    def set_status(self, institution_id: str, asset_id: str, *, status: str, verified_via: str | None = None, verified_at: str | None = None) -> None:
        if status not in ASSET_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._tenant(institution_id):
            if verified_via:
                self.backend.execute("UPDATE intel_assets SET status = ?, last_verified_at = ?, last_verified_via = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?", (status, verified_at or now_iso(), verified_via, now_iso(), institution_id, asset_id))
            else:
                self.backend.execute("UPDATE intel_assets SET status = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?", (status, now_iso(), institution_id, asset_id))

    def set_observation(
        self, institution_id: str, asset_id: str, *, platform_id: str | None = None, followers: int | None = None, followers_source: str | None = None,
        last_activity_at: str | None = None, registration_expires_at: str | None = None,
    ) -> None:
        """Record what a platform or registry reports about an asset; values left None are kept."""

        with self._tenant(institution_id):
            self.backend.execute(
                # The follower fields move together; a Python-side flag decides,
                # because PostgreSQL cannot type a bare "? IS NULL" probe.
                "UPDATE intel_assets SET platform_id = COALESCE(?, platform_id), followers = COALESCE(?, followers), followers_source = CASE WHEN ? = 1 THEN ? ELSE followers_source END, "
                "followers_at = CASE WHEN ? = 1 THEN ? ELSE followers_at END, last_activity_at = COALESCE(?, last_activity_at), registration_expires_at = COALESCE(?, registration_expires_at), updated_at = ? "
                "WHERE institution_id = ? AND asset_id = ?",
                (platform_id, followers, int(followers is not None), followers_source, int(followers is not None), now_iso(), last_activity_at, registration_expires_at, now_iso(), institution_id, asset_id),
            )

    # ---------------------------------------------------------------- evidence
    def add_evidence(
        self, institution_id: str, *, asset_id: str, kind: str, polarity: str = "supports", source_url: str = "", source_asset_id: str | None = None, detail: str = "",
        channel: str = "", observed_via: str = "live", run_id: str | None = None, raw_sha256: str | None = None, observed_at: str | None = None,
    ) -> str:
        if polarity not in {"supports", "refutes"}:
            raise ValueError("polarity must be supports or refutes")
        if observed_via not in {"live", "index", "archive", "owner", "reviewer", "import"}:
            raise ValueError(f"unknown observation channel {observed_via!r}")
        if kind in FREE_TEXT_EVIDENCE:
            detail = self._without_names(institution_id, detail)
        evidence_id = f"ievd-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO intel_evidence(evidence_id, institution_id, asset_id, kind, polarity, source_url, source_asset_id, detail, channel, observed_via, run_id, raw_sha256, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, institution_id, asset_id, kind, polarity, source_url[:1000], source_asset_id, detail[:500], channel[:100], observed_via, run_id, raw_sha256, observed_at or now_iso()),
            )
        return evidence_id

    def list_evidence(self, institution_id: str, *, asset_id: str | None = None, since: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        """The newest ``limit`` evidence rows, oldest first (for display; grading uses ``evidence_for``)."""

        clauses, params = ["institution_id = ?"], [institution_id]
        if asset_id:
            clauses.append("asset_id = ?")
            params.append(asset_id)
        if since:
            clauses.append("observed_at >= ?")
            params.append(since)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM intel_evidence WHERE {' AND '.join(clauses)} ORDER BY observed_at DESC, evidence_id DESC LIMIT ?", (*params, max(1, min(limit, 20000))))
        return rows[::-1]

    def evidence_for(self, institution_id: str, asset_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Every evidence row of the given assets, oldest first, with no cap: a grade must see all of it."""

        wanted = list(dict.fromkeys(asset_ids))
        found: dict[str, list[dict[str, Any]]] = {asset_id: [] for asset_id in wanted}
        with self._tenant(institution_id):
            for chunk in iter_chunks(wanted, 200):
                marks = ", ".join("?" for _ in chunk)
                for row in self.backend.fetchall(f"SELECT * FROM intel_evidence WHERE institution_id = ? AND asset_id IN ({marks}) ORDER BY observed_at, evidence_id", (institution_id, *chunk)):
                    found[row["asset_id"]].append(row)
        return found

    def links_from(self, institution_id: str, source_asset_id: str, *, kind: str = "official_link", source_urls: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Every ``kind`` row a source (an official domain) gave, oldest first, optionally for some of its pages."""

        clauses, params = ["institution_id = ?", "source_asset_id = ?", "kind = ?"], [institution_id, source_asset_id, kind]
        urls = list(dict.fromkeys(source_urls)) if source_urls is not None else None
        if urls is not None:
            if not urls:
                return []
            clauses.append(f"source_url IN ({', '.join('?' for _ in urls)})")
            params.extend(urls)
        with self._tenant(institution_id):
            return self.backend.fetchall(f"SELECT * FROM intel_evidence WHERE {' AND '.join(clauses)} ORDER BY observed_at, evidence_id", tuple(params))

    def has_evidence(self, institution_id: str, asset_id: str, kind: str) -> bool:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT 1 AS present FROM intel_evidence WHERE institution_id = ? AND asset_id = ? AND kind = ? LIMIT 1", (institution_id, asset_id, kind)) is not None

    # -------------------------------------------------------------------- gold
    def add_gold(self, institution_id: str, *, asset_key: str, platform: str, split: str, entity_name: str = "", relation: str = "unknown", expected_min_grade: str = "C", source: str = "") -> bool:
        """Record a ground-truth item; returns False when the key is already in the gold set."""

        if split not in SPLITS:
            raise ValueError(f"unknown split {split!r}")
        if expected_min_grade not in GRADE_RANK:
            raise ValueError(f"unknown grade {expected_min_grade!r}")
        with self._tenant(institution_id):
            if self.backend.fetchone("SELECT 1 AS present FROM intel_gold_items WHERE institution_id = ? AND asset_key = ?", (institution_id, asset_key)):
                return False
            self.backend.execute(
                "INSERT INTO intel_gold_items(gold_id, institution_id, asset_key, platform, entity_name, relation, split, expected_min_grade, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"igld-{uuid4().hex}", institution_id, asset_key, platform, entity_name[:200], relation, split, expected_min_grade, source[:200], now_iso()),
            )
        return True

    def make_canary(self, institution_id: str, asset_key: str, *, entity_name: str) -> bool:
        """Turn an existing seed or holdout gold item into a canary (a look-alike that must never grade official)."""

        with self._tenant(institution_id):
            return bool(self.backend.execute(
                "UPDATE intel_gold_items SET split = 'canary', relation = 'third_party', expected_min_grade = 'D', entity_name = ? WHERE institution_id = ? AND asset_key = ? AND split <> 'canary'",
                (entity_name[:200], institution_id, asset_key),
            ))

    def batch(self, institution_id: str):
        """One tenant transaction around many store calls (they nest into it)."""

        return self._tenant(institution_id)

    def list_gold(self, institution_id: str, *, split: str | None = None) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        if split:
            clauses.append("split = ?")
            params.append(split)
        with self._tenant(institution_id):
            return self.backend.fetchall(f"SELECT * FROM intel_gold_items WHERE {' AND '.join(clauses)} ORDER BY asset_key", tuple(params))

    def delete_gold(self, institution_id: str, asset_key: str) -> int:
        """Drop a ground-truth item (a suppressed account must not stay behind in plaintext, hidden or not)."""

        with self._tenant(institution_id):
            return self.backend.execute("DELETE FROM intel_gold_items WHERE institution_id = ? AND asset_key = ?", (institution_id, asset_key))

    def holdout_keys(self, institution_id: str) -> frozenset[str]:
        return frozenset(item["asset_key"] for item in self.list_gold(institution_id, split="holdout"))

    # ------------------------------------------------------------- suppression
    def suppress(self, institution_id: str, identifier: str, *, reason: str) -> str:
        """Record that an identifier (a personal account) must never enter the map."""

        fingerprint = suppression_fingerprint(self.suppression_key, identifier)
        with self._tenant(institution_id):
            if not self.backend.fetchone("SELECT 1 AS present FROM intel_suppression WHERE institution_id = ? AND key_hmac = ?", (institution_id, fingerprint)):
                self.backend.execute("INSERT INTO intel_suppression(institution_id, key_hmac, reason, created_at) VALUES (?, ?, ?, ?)", (institution_id, fingerprint, reason[:200], now_iso()))
        return fingerprint

    def is_suppressed(self, institution_id: str, identifier: str) -> bool:
        fingerprint = suppression_fingerprint(self.suppression_key, identifier)
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT 1 AS present FROM intel_suppression WHERE institution_id = ? AND key_hmac = ?", (institution_id, fingerprint)) is not None

    # ---------------------------------------------------------- owner tokens
    def owner_token_epoch(self, institution_id: str) -> int:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT epoch FROM intel_owner_tokens WHERE institution_id = ?", (institution_id,))
        return int(row["epoch"]) if row else 0

    def rotate_owner_token(self, institution_id: str, *, by: str) -> int:
        """Move the institution to its next token epoch; returns the new epoch."""

        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO intel_owner_tokens(institution_id, epoch, rotated_by, rotated_at) VALUES (?, 1, ?, ?) "
                "ON CONFLICT (institution_id) DO UPDATE SET epoch = intel_owner_tokens.epoch + 1, rotated_by = excluded.rotated_by, rotated_at = excluded.rotated_at",
                (institution_id, by[:200], now_iso()),
            )
            row = self.backend.fetchone("SELECT epoch FROM intel_owner_tokens WHERE institution_id = ?", (institution_id,))
        return int(row["epoch"]) if row else 0

    # -------------------------------------------------------------------- runs
    def start_map_run(self, institution_id: str, *, kind: str = "tick") -> str:
        run_id = f"imrn-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute("INSERT INTO intel_map_runs(run_id, institution_id, kind, started_at, status) VALUES (?, ?, ?, ?, 'running')", (run_id, institution_id, kind, now_iso()))
        return run_id

    def finish_map_run(
        self, institution_id: str, run_id: str, *, status: str, stop_reason: str | None = None, gate: str | None = None, spend: Mapping[str, Any] | None = None,
        counts: Mapping[str, Any] | None = None, metrics: Mapping[str, Any] | None = None, error: str | None = None,
    ) -> None:
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE intel_map_runs SET finished_at = ?, status = ?, stop_reason = ?, gate = ?, spend_json = ?, counts_json = ?, metrics_json = ?, error = ? WHERE institution_id = ? AND run_id = ?",
                (now_iso(), status, stop_reason, gate, _json(dict(spend or {})), _json(dict(counts or {})), _json(dict(metrics or {})), error, institution_id, run_id),
            )

    def list_map_runs(self, institution_id: str, *, kind: str | None = None, status: str | None = None, since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Runs, newest first; ``since`` filters in SQL, so a window's runs are all there (up to 10,000) however often ticks run."""

        clauses, params = ["institution_id = ?"], [institution_id]
        for column, value in (("kind", kind), ("status", status)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM intel_map_runs WHERE {' AND '.join(clauses)} ORDER BY started_at DESC, run_id DESC LIMIT ?", (*params, max(1, min(limit, 10_000 if since else 500))))
        for row in rows:
            for column in ("spend", "counts", "metrics"):
                row[column] = _loads(row.pop(f"{column}_json", "{}"), {})
        return rows

    def stale_running_runs(self, institution_id: str, *, older_than_seconds: int) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT run_id FROM intel_map_runs WHERE institution_id = ? AND status = 'running' AND started_at < ?", (institution_id, cutoff))
        return [str(row["run_id"]) for row in rows]

    # --------------------------------------------------------------- retention
    def prune_retention(self, *, now: datetime | None = None, days: int, institution_id: str | None = None) -> dict[str, int]:
        """Delete or blank what the map no longer needs ``days`` after it was last current; returns counts.

        Only closed or superseded work goes: decided or expired review items,
        resolved incidents, runs (except the newest baseline, which later runs
        are compared with), fetch validators not refreshed, sources the map
        stopped following, and quota and spend rows (after a month at most).
        The map itself (entities, assets, ground truth, suppression) stays, and
        so does the evidence every grade is recomputed from: past the period a
        free-text detail is blanked and liveness and integrity observations that
        newer ones superseded are deleted, keeping every row the grader reads
        (see grading.FREE_TEXT_KINDS and grading.superseded_observations). Each
        institution is pruned in its own tenant transaction, so row-level
        security applies; with no ``institution_id`` every institution that
        known_tenants finds is pruned in turn.
        """

        if days < 1:
            raise ValueError("days must be positive")
        current = now or datetime.now(timezone.utc)
        current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        before = current - timedelta(days=days)
        ledger_day = (current - timedelta(days=min(days, LEDGER_RETENTION_DAYS))).date().isoformat()
        totals = dict.fromkeys((*RETENTION_COUNTS, "budget_ledger", "institutions"), 0)
        for tenant in [institution_id] if institution_id is not None else known_tenants(self.backend, TENANT_TABLES):
            for key, count in self._prune_tenant(tenant, before=before, ledger_day=ledger_day).items():
                totals[key] += count
            totals["institutions"] += 1
        # Platform-wide spend is not tenant data; pruning it again for each institution's pass costs one indexed delete.
        with self.backend.transaction():
            bound_lock_waits(self.backend)
            totals["budget_ledger"] = self.backend.execute("DELETE FROM intel_budget_ledger WHERE day < ?", (ledger_day,))
        return totals

    def _prune_tenant(self, institution_id: str, *, before: datetime, ledger_day: str) -> dict[str, int]:
        cutoff = before.isoformat()
        with self._tenant(institution_id):
            # Start-up prunes too: a row a stuck writer holds fails the pass instead of hanging it.
            bound_lock_waits(self.backend)
            # A decision older than the period is forgotten (the same item found again is asked
            # again); the evidence it added stays, so the grade it caused stays too.
            return {
                "review_items": self.backend.execute("DELETE FROM intel_review_items WHERE institution_id = ? AND status IN ('decided', 'expired') AND COALESCE(decided_at, updated_at) < ?", (institution_id, cutoff)),
                "incidents": self.backend.execute("DELETE FROM intel_incidents WHERE institution_id = ? AND status = 'resolved' AND COALESCE(resolved_at, last_seen_at) < ?", (institution_id, cutoff)),
                "map_runs": self.backend.execute(
                    "DELETE FROM intel_map_runs WHERE institution_id = ? AND started_at < ? AND run_id NOT IN (SELECT run_id FROM intel_map_runs WHERE institution_id = ? AND kind = 'baseline' ORDER BY started_at DESC, run_id DESC LIMIT 1)",
                    (institution_id, cutoff, institution_id),
                ),
                "quota": self.backend.execute("DELETE FROM intel_quota WHERE institution_id = ? AND day < ?", (institution_id, ledger_day)),
                "fetch_validators": self.backend.execute("DELETE FROM intel_fetch_validators WHERE institution_id = ? AND fetched_at < ?", (institution_id, cutoff)),
                # A lead found again after this is followed afresh; an expired one never produced anything anyway.
                "sources": self.backend.execute("DELETE FROM intel_sources WHERE institution_id = ? AND status IN ('expired', 'pruned') AND updated_at < ?", (institution_id, cutoff)),
                "evidence_blanked": self._expire_free_text(institution_id, cutoff),
                "observations": self._drop_superseded_observations(institution_id, before),
            }

    def _expire_free_text(self, institution_id: str, cutoff: str) -> int:
        """Blank the free text of evidence observed before ``cutoff``, and the quotes of it in stored grade reasons.

        Kind, polarity, channel, source, time and observation channel stay,
        and they are all the grader reads of these kinds, so every grade stays
        and the stored reasons read as the next regrade would word them.
        Batched, so a first pass over a long backlog holds little in memory.
        """

        from .grading import EXPIRED_DETAIL, FREE_TEXT_KINDS, expire_quotes  # grading imports this module

        kinds, page, blanked = sorted(FREE_TEXT_KINDS), 1000, 0
        while True:
            rows = self.backend.fetchall(
                f"SELECT evidence_id, asset_id, detail FROM intel_evidence WHERE institution_id = ? AND observed_at < ? AND kind IN ({', '.join('?' for _ in kinds)}) AND detail <> '' AND detail <> ? LIMIT ?",
                (institution_id, cutoff, *kinds, EXPIRED_DETAIL, page),
            )
            for chunk in iter_chunks([row["evidence_id"] for row in rows], 200):
                self.backend.execute(f"UPDATE intel_evidence SET detail = ? WHERE institution_id = ? AND evidence_id IN ({', '.join('?' for _ in chunk)})", (EXPIRED_DETAIL, institution_id, *chunk))
            quoted: dict[str, list[str]] = {}
            for row in rows:
                quoted.setdefault(str(row["asset_id"]), []).append(str(row["detail"]))
            for chunk in iter_chunks(list(quoted), 200):
                for asset in self.backend.fetchall(f"SELECT asset_id, grade_reasons_json FROM intel_assets WHERE institution_id = ? AND asset_id IN ({', '.join('?' for _ in chunk)})", (institution_id, *chunk)):
                    reasons = _loads(asset["grade_reasons_json"], [])
                    expired = expire_quotes(reasons, quoted[str(asset["asset_id"])])
                    if expired != reasons:
                        self.backend.execute("UPDATE intel_assets SET grade_reasons_json = ? WHERE institution_id = ? AND asset_id = ?", (_json(expired), institution_id, asset["asset_id"]))
            blanked += len(rows)
            if len(rows) < page:
                return blanked

    def _drop_superseded_observations(self, institution_id: str, before: datetime) -> int:
        """Delete the liveness and integrity rows observed before ``before`` that the grader no longer reads."""

        from .grading import superseded_observations  # grading imports this module

        assets = [str(row["asset_id"]) for row in self.backend.fetchall("SELECT DISTINCT asset_id FROM intel_evidence WHERE institution_id = ? AND kind IN ('liveness', 'integrity') AND observed_at < ?", (institution_id, before.isoformat()))]
        deleted = 0
        # A few assets at a time: a site checked daily for a year has hundreds of rows.
        for chunk in iter_chunks(assets, 50):
            rows = self.backend.fetchall(
                f"SELECT evidence_id, asset_id, kind, polarity, channel, detail, observed_via, observed_at FROM intel_evidence WHERE institution_id = ? AND kind IN ('liveness', 'integrity') AND asset_id IN ({', '.join('?' for _ in chunk)})",
                (institution_id, *chunk),
            )
            for doomed in iter_chunks(superseded_observations(rows, before=before), 200):
                deleted += self.backend.execute(f"DELETE FROM intel_evidence WHERE institution_id = ? AND evidence_id IN ({', '.join('?' for _ in doomed)})", (institution_id, *doomed))
        return deleted


def grade_at_least(grade: str | None, minimum: str) -> bool:
    return GRADE_RANK.get(grade or "unrated", 1) >= GRADE_RANK[minimum]


def iter_chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


__all__ = [
    "ADDED_COLUMNS", "ASSET_STATUSES", "ENTITY_KINDS", "FREE_TEXT_EVIDENCE", "GLOBAL_TABLES", "GRADES", "GRADE_RANK", "LEDGER_RETENTION_DAYS", "RELATIONS", "RETENTION_COUNTS", "SCHEMA_VERSION", "SOURCE_CLASSES",
    "SOURCE_ORIGINS", "SPLITS", "TENANT_TABLES", "MapStore", "grade_at_least", "iter_chunks", "render_sql_migration", "suppression_fingerprint",
]
