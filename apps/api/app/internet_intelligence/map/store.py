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
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ...persistence.schema_tools import add_missing_columns, apply_schema, begin_migration, existing_policies, row_level_security_state, tenant_isolation_statements
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
ASSET_STATUSES = frozenset({"unknown", "live", "blocked", "dead", "parked", "hijacked", "compromised", "disputed"})
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
)
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = ()
TENANT_TABLES: tuple[str, ...] = ("intel_entities", "intel_assets", "intel_evidence", "intel_gold_items", "intel_suppression", "intel_map_runs")


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
    lines.extend(statement + ";" for statement in tenant_isolation_statements(TENANT_TABLES, state={}, policies=()))
    return "\n".join(lines) + "\n"


class MapStore:
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

    def set_observation(self, institution_id: str, asset_id: str, *, platform_id: str | None = None, followers: int | None = None, followers_source: str | None = None) -> None:
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE intel_assets SET platform_id = COALESCE(?, platform_id), followers = COALESCE(?, followers), followers_source = CASE WHEN ? IS NULL THEN followers_source ELSE ? END, followers_at = CASE WHEN ? IS NULL THEN followers_at ELSE ? END, updated_at = ? WHERE institution_id = ? AND asset_id = ?",
                (platform_id, followers, followers, followers_source, followers, now_iso(), now_iso(), institution_id, asset_id),
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
        clauses, params = ["institution_id = ?"], [institution_id]
        if asset_id:
            clauses.append("asset_id = ?")
            params.append(asset_id)
        if since:
            clauses.append("observed_at >= ?")
            params.append(since)
        with self._tenant(institution_id):
            return self.backend.fetchall(f"SELECT * FROM intel_evidence WHERE {' AND '.join(clauses)} ORDER BY observed_at, evidence_id LIMIT ?", (*params, max(1, min(limit, 20000))))

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
    "ADDED_COLUMNS", "ASSET_STATUSES", "ENTITY_KINDS", "GRADES", "GRADE_RANK", "RELATIONS", "SCHEMA_VERSION", "SPLITS", "TENANT_TABLES", "MapStore", "grade_at_least", "iter_chunks",
    "render_sql_migration", "suppression_fingerprint",
]
