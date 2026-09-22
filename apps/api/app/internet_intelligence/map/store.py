"""The internet map's memory: entities, assets, the evidence log and how it is measured.

Everything is tenant data under forced row-level security on PostgreSQL. The
evidence table is append-only (there is no update or delete method): a grade
is always recomputed from it, so any grade can be explained and replayed.
Tables carry an ``intel_`` prefix because this store shares its database with
the institution-data store, which already owns generic names such as
``review_items``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ...persistence.schema_tools import add_missing_columns, apply_schema, begin_migration, existing_policies, idempotent_tenant_isolation_sql, row_level_security_state, tenant_isolation_statements
from ...persistence.sql_backend import SqlBackend, open_backend
from .assets import AssetRef

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
    """
    CREATE TABLE IF NOT EXISTS intel_fetch_state (
        url_sha256 TEXT PRIMARY KEY,
        etag TEXT,
        last_modified TEXT,
        outcome TEXT NOT NULL,
        content_sha256 TEXT,
        fetched_at TEXT NOT NULL
    )
    """,
)
# Columns added after the table first existed; created where missing at start-up.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("intel_assets", "last_activity_at", "TEXT"),
    ("intel_assets", "registration_expires_at", "TEXT"),
)
TENANT_TABLES: tuple[str, ...] = ("intel_entities", "intel_assets", "intel_evidence", "intel_gold_items", "intel_suppression", "intel_map_runs", "intel_sources", "intel_quota")
GLOBAL_TABLES: tuple[str, ...] = ("intel_budget_ledger", "intel_fetch_state")
SOURCE_CLASSES = frozenset({"rotation", "recheck", "explore"})
SOURCE_ORIGINS = frozenset({"seed", "recurring", "lead", "gap", "profile"})


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
    lines = [f"-- Guru Ji internet map schema {SCHEMA_VERSION}", "-- Generated from app.internet_intelligence.map.store; edit the store, not this file."]
    lines.extend(" ".join(statement.split()) + ";" for statement in _STATEMENTS)
    lines.extend(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type};" for table, column, column_type in ADDED_COLUMNS)
    lines.append("-- PostgreSQL row-level security (skipped on SQLite)")
    lines.extend(statement + ";" for statement in idempotent_tenant_isolation_sql(TENANT_TABLES))
    return "\n".join(lines) + "\n"


class MapStoreScheduling:
    """Sources, leases, budgets and the shared fetch cache (mixed into MapStore)."""

    backend: SqlBackend

    def _tenant(self, institution_id: str):  # pragma: no cover - provided by MapStore
        raise NotImplementedError

    # ---------------------------------------------------------------- sources
    def upsert_source(
        self, institution_id: str, *, connector: str, target: str, entity_id: str | None = None, asset_id: str | None = None, topic: str = "", origin: str = "seed",
        work_class: str = "rotation", hops: int = 0, interval_seconds: int = 86400, due_at: str | None = None, expires_at: str | None = None,
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
                "INSERT INTO intel_sources(source_id, institution_id, connector, target, entity_id, asset_id, topic, origin, work_class, hops, due_at, interval_seconds, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, institution_id, connector, target, entity_id, asset_id, topic[:100], origin, work_class, max(0, hops), due_at or stamp, max(60, int(interval_seconds)), expires_at, stamp, stamp),
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

    def claim_due(self, institution_id: str, *, now: str, worker: str, lease_seconds: int, limit: int, work_class: str | None = None, connectors: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Lease up to ``limit`` due sources; a source leased by another live worker is skipped.

        The lease is written with a condition on the lease columns, so two
        workers racing for the same source cannot both win it.
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
            candidates = self.backend.fetchall(f"SELECT * FROM intel_sources WHERE {' AND '.join(clauses)} ORDER BY due_at, source_id LIMIT ?", (*params, max(1, limit * 3)))
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

    def complete_source(
        self, institution_id: str, source_id: str, *, outcome: str, next_due: str, interval_seconds: int, yield_count: int, cost: float, failed: bool,
        etag: str | None = None, last_modified: str | None = None, status: str | None = None, origin: str | None = None, clear_expiry: bool = False,
    ) -> None:
        with self._tenant(institution_id):
            if clear_expiry:
                self.backend.execute("UPDATE intel_sources SET expires_at = NULL WHERE institution_id = ? AND source_id = ?", (institution_id, source_id))
            self.backend.execute(
                """
                UPDATE intel_sources SET last_outcome = ?, due_at = ?, interval_seconds = ?, yield_last = ?, yield_total = yield_total + ?, cost_total = cost_total + ?, runs = runs + 1,
                    failure_streak = CASE WHEN ? = 1 THEN failure_streak + 1 ELSE 0 END, etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),
                    status = COALESCE(?, status), origin = COALESCE(?, origin), lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE institution_id = ? AND source_id = ?
                """,
                (outcome[:60], next_due, max(60, int(interval_seconds)), yield_count, yield_count, float(cost), 1 if failed else 0, etag, last_modified, status, origin, now_iso(), institution_id, source_id),
            )

    def set_source_status(self, institution_id: str, source_id: str, status: str) -> None:
        with self._tenant(institution_id):
            self.backend.execute("UPDATE intel_sources SET status = ?, lease_owner = NULL, lease_until = NULL, updated_at = ? WHERE institution_id = ? AND source_id = ?", (status, now_iso(), institution_id, source_id))

    def expire_sources(self, institution_id: str, *, now: str) -> int:
        """Leads past their expiry that never produced anything stop being scheduled (kept for audit)."""

        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE intel_sources SET status = 'expired', updated_at = ? WHERE institution_id = ? AND status = 'active' AND expires_at IS NOT NULL AND expires_at < ? AND yield_total = 0",
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

    def spend(self, *, day: str) -> dict[str, dict[str, float]]:
        with self.backend.transaction():
            rows = self.backend.fetchall("SELECT connector, units, calls FROM intel_budget_ledger WHERE day = ? ORDER BY connector", (day,))
        return {str(row["connector"]): {"units": float(row["units"]), "calls": int(row["calls"])} for row in rows}

    def tenant_spend(self, institution_id: str, *, day: str) -> dict[str, dict[str, float]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT connector, units, calls FROM intel_quota WHERE institution_id = ? AND day = ? ORDER BY connector", (institution_id, day))
        return {str(row["connector"]): {"units": float(row["units"]), "calls": int(row["calls"])} for row in rows}

    # -------------------------------------------------------------- fetch cache
    def fetch_state(self, url: str) -> dict[str, Any] | None:
        with self.backend.transaction():
            return self.backend.fetchone("SELECT * FROM intel_fetch_state WHERE url_sha256 = ?", (hashlib.sha256(url.encode("utf-8")).hexdigest(),))

    def record_fetch(self, url: str, *, outcome: str, etag: str | None, last_modified: str | None, content_sha256: str | None) -> None:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with self.backend.transaction():
            self.backend.execute(
                "INSERT INTO intel_fetch_state(url_sha256, etag, last_modified, outcome, content_sha256, fetched_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (url_sha256) DO UPDATE SET etag = excluded.etag, last_modified = excluded.last_modified, outcome = excluded.outcome, content_sha256 = COALESCE(excluded.content_sha256, intel_fetch_state.content_sha256), fetched_at = excluded.fetched_at",
                (key, etag, last_modified, outcome[:40], content_sha256, now_iso()),
            )

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


class MapStore(MapStoreScheduling):
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
                    "UPDATE intel_entities SET names_json = ?, locations_json = ?, group_label = CASE WHEN ? = '' THEN group_label ELSE ? END, parent_id = COALESCE(?, parent_id), updated_at = ? WHERE institution_id = ? AND entity_id = ?",
                    (_json(merged_names), _json(merged_locations), group_label, group_label, parent_id, stamp, institution_id, existing["entity_id"]),
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

    def set_grade(self, institution_id: str, asset_id: str, *, grade: str, reasons: Sequence[str], scorer_version: str, proposed: bool = False) -> None:
        """Store a computed grade; ``proposed`` parks it until a run gate lets it through."""

        if grade not in GRADE_RANK:
            raise ValueError(f"unknown grade {grade!r}")
        with self._tenant(institution_id):
            if proposed:
                self.backend.execute("UPDATE intel_assets SET proposed_grade = ?, grade_reasons_json = ?, scorer_version = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?", (grade, _json(list(reasons)), scorer_version, now_iso(), institution_id, asset_id))
            else:
                self.backend.execute("UPDATE intel_assets SET grade = ?, proposed_grade = NULL, grade_reasons_json = ?, scorer_version = ?, updated_at = ? WHERE institution_id = ? AND asset_id = ?", (grade, _json(list(reasons)), scorer_version, now_iso(), institution_id, asset_id))

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

    def list_map_runs(self, institution_id: str, *, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses, params = ["institution_id = ?"], [institution_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM intel_map_runs WHERE {' AND '.join(clauses)} ORDER BY started_at DESC, run_id DESC LIMIT ?", (*params, max(1, min(limit, 500))))
        for row in rows:
            for column in ("spend", "counts", "metrics"):
                row[column] = _loads(row.pop(f"{column}_json", "{}"), {})
        return rows

    def stale_running_runs(self, institution_id: str, *, older_than_seconds: int) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT run_id FROM intel_map_runs WHERE institution_id = ? AND status = 'running' AND started_at < ?", (institution_id, cutoff))
        return [str(row["run_id"]) for row in rows]


def grade_at_least(grade: str | None, minimum: str) -> bool:
    return GRADE_RANK.get(grade or "unrated", 1) >= GRADE_RANK[minimum]


def iter_chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


__all__ = [
    "ADDED_COLUMNS", "ASSET_STATUSES", "ENTITY_KINDS", "GLOBAL_TABLES", "GRADES", "GRADE_RANK", "RELATIONS", "SCHEMA_VERSION", "SOURCE_CLASSES", "SOURCE_ORIGINS", "SPLITS", "TENANT_TABLES", "MapStore", "grade_at_least", "iter_chunks",
    "render_sql_migration", "suppression_fingerprint",
]
