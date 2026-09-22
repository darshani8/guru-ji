"""Intelligence evidence store, kept apart from operational institution data."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..persistence.schema_tools import add_missing_columns, apply_schema, begin_migration, existing_policies, row_level_security_state, tenant_isolation_statements
from ..persistence.sql_backend import SqlBackend, open_backend
from .profile import InstitutionProfile

SCHEMA_VERSION = "003_internet_intelligence"

_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS institution_profiles (
        institution_id TEXT PRIMARY KEY,
        profile_json TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS internet_documents (
        document_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        url TEXT NOT NULL,
        domain TEXT NOT NULL,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        published_at TEXT,
        date_status TEXT NOT NULL DEFAULT 'unknown',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        match_level TEXT NOT NULL,
        match_score REAL NOT NULL,
        match_reasons_json TEXT NOT NULL DEFAULT '[]',
        relevance_score REAL NOT NULL DEFAULT 0,
        topics_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        extracted INTEGER NOT NULL DEFAULT 0,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE(institution_id, canonical_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_internet_documents_institution ON internet_documents(institution_id, last_seen_at)",
    """
    CREATE TABLE IF NOT EXISTS monitoring_runs (
        run_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        queries_json TEXT NOT NULL DEFAULT '[]',
        new_count INTEGER NOT NULL DEFAULT 0,
        changed_count INTEGER NOT NULL DEFAULT 0,
        duplicate_count INTEGER NOT NULL DEFAULT 0,
        excluded_count INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitoring_runs_institution ON monitoring_runs(institution_id, started_at)",
    """
    CREATE TABLE IF NOT EXISTS monitoring_events (
        event_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        importance TEXT NOT NULL,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        alerted INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitoring_events_institution ON monitoring_events(institution_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS intelligence_reports (
        report_id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        question TEXT,
        window_days INTEGER NOT NULL,
        findings_count INTEGER NOT NULL,
        summary TEXT NOT NULL,
        findings_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intelligence_reports_institution ON intelligence_reports(institution_id, created_at)",
)
# Provenance added after the tables first shipped: why a row has its status,
# which provider and queries found it, the URL the provider returned before
# redirects, and the run that last saw it.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("internet_documents", "status_reason", "TEXT"),
    ("internet_documents", "provider", "TEXT"),
    ("internet_documents", "queries_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("internet_documents", "requested_url", "TEXT"),
    ("internet_documents", "last_run_id", "TEXT"),
    ("monitoring_runs", "stop_reason", "TEXT"),
)
# A monitoring run still marked running after this long lost its process; it
# no longer holds the per-institution lock and is closed as abandoned.
RUN_LOCK_SECONDS = 1800
# institution_profiles is deliberately not under row-level security: the monitoring
# scheduler enumerates every profile with monitoring enabled without a tenant
# context, and the store's own methods always filter profiles by institution_id.
_TENANT_TABLES = ("internet_documents", "monitoring_runs", "monitoring_events", "intelligence_reports")


def _rls(state: Mapping[str, tuple[bool, bool]], policies: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """Row-level security statements the catalog shows are still missing.

    Every ``ALTER TABLE`` takes an ACCESS EXCLUSIVE lock even when it changes
    nothing, and the previous release is still serving while a new one
    starts, so a converged database gets no statement at all.
    """

    statements: list[str] = []
    # Earlier revisions placed institution_profiles under forced RLS, which hid
    # every profile from the tenant-less scheduler; undo that so an existing
    # database converges on the same shape as a fresh one.
    enabled, forced = state.get("institution_profiles", (False, False))
    if forced:
        statements.append("ALTER TABLE institution_profiles NO FORCE ROW LEVEL SECURITY")
    if enabled:
        statements.append("ALTER TABLE institution_profiles DISABLE ROW LEVEL SECURITY")
    if ("institution_profiles", "institution_profiles_tenant_isolation") in set(policies):
        statements.append("DROP POLICY IF EXISTS institution_profiles_tenant_isolation ON institution_profiles")
    statements.extend(tenant_isolation_statements(_TENANT_TABLES, state=state, policies=policies))
    return tuple(statements)


def render_sql_migration() -> str:
    lines = [f"-- Guru Ji internet intelligence schema {SCHEMA_VERSION}", "-- Generated from app.internet_intelligence.store; edit the store, not this file."]
    lines.extend(" ".join(statement.split()) + ";" for statement in _STATEMENTS)
    lines.extend(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type};" for table, column, column_type in ADDED_COLUMNS)
    lines.append("-- PostgreSQL row-level security (skipped on SQLite); institution_profiles stays outside it for the scheduler")
    lines.extend(statement + ";" for statement in tenant_isolation_statements(_TENANT_TABLES, state={}, policies=()))
    return "\n".join(lines) + "\n"


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


class IntelligenceStore:
    def __init__(self, database_url: str | None = None, backend: SqlBackend | None = None) -> None:
        self.backend = backend or open_backend(database_url)
        self.backend_name = self.backend.dialect
        self._migrate()

    def _migrate(self) -> None:
        # One transaction under lock_timeout, issuing DDL only for what the
        # catalog shows missing; see persistence.schema_tools.
        with self.backend.transaction():
            begin_migration(self.backend)
            apply_schema(self.backend, _STATEMENTS)
            add_missing_columns(self.backend, ADDED_COLUMNS)
            if self.backend.dialect == "postgresql":
                for statement in _rls(row_level_security_state(self.backend), existing_policies(self.backend)):
                    self.backend.execute(statement)
            self.backend.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT (version) DO NOTHING", (SCHEMA_VERSION, now_iso()))

    def close(self) -> None:
        self.backend.close()

    def _tenant(self, institution_id: str):
        if not institution_id.strip():
            raise ValueError("institution_id is required")
        return self.backend.transaction(tenant_id=institution_id)

    # ---------------------------------------------------------------- profiles
    def save_profile(self, profile: InstitutionProfile, *, updated_by: str) -> InstitutionProfile:
        with self._tenant(profile.institution_id):
            self.backend.execute(
                "INSERT INTO institution_profiles(institution_id, profile_json, updated_by, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (institution_id) DO UPDATE SET profile_json = EXCLUDED.profile_json, updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                (profile.institution_id, _json(profile.as_dict()), updated_by, now_iso()),
            )
        return profile

    def get_profile(self, institution_id: str) -> InstitutionProfile | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT profile_json FROM institution_profiles WHERE institution_id = ?", (institution_id,))
        if row is None:
            return None
        return InstitutionProfile.from_dict(institution_id, _loads(row["profile_json"], {}))

    def monitored_institutions(self) -> list[str]:
        """Institutions with monitoring enabled; a platform-level read used by the scheduler."""

        rows = self.backend.fetchall("SELECT institution_id, profile_json FROM institution_profiles ORDER BY institution_id")
        return [row["institution_id"] for row in rows if _loads(row["profile_json"], {}).get("monitoring_enabled")]

    # --------------------------------------------------------------- evidence
    def upsert_document(self, institution_id: str, item: Mapping[str, Any]) -> tuple[str, str]:
        """Insert or refresh evidence; returns (document_id, 'new' | 'changed' | 'duplicate').

        Two refreshes do not count as a change. A page seen once through its
        full text and once through the search snippet (the fetch failed this
        time) hashes differently without having changed, so a switch between
        the two keeps the richer copy and reports a duplicate. And a shorter
        search window must not undo a longer one: a row another run kept stays
        kept when this run only excluded it for being outside its window.
        """

        stamp = now_iso()
        queries = _json(list(dict.fromkeys(item.get("queries", []))))
        with self._tenant(institution_id):
            existing = self.backend.fetchone(
                "SELECT document_id, content_sha256, extracted, status, status_reason, title, excerpt FROM internet_documents WHERE institution_id = ? AND canonical_url = ?",
                (institution_id, item["canonical_url"]),
            )
            if existing is None:
                document_id = f"idoc-{uuid4().hex}"
                self.backend.execute(
                    """
                    INSERT INTO internet_documents(document_id, institution_id, canonical_url, url, domain, source_type, title, excerpt, content_sha256, published_at, date_status,
                        first_seen_at, last_seen_at, retrieved_at, match_level, match_score, match_reasons_json, relevance_score, topics_json, status, extracted, warnings_json,
                        status_reason, provider, queries_json, requested_url, last_run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id, institution_id, item["canonical_url"], item["url"], item["domain"], item["source_type"], item["title"][:300], item["excerpt"][:2000], item["content_sha256"],
                        item.get("published_at"), item.get("date_status", "unknown"), stamp, stamp, item.get("retrieved_at", stamp), item["match_level"], float(item["match_score"]),
                        _json(list(item.get("match_reasons", []))), float(item.get("relevance_score", 0.0)), _json(list(item.get("topics", []))), item["status"], 1 if item.get("extracted") else 0, _json(list(item.get("warnings", []))),
                        item.get("status_reason"), item.get("provider"), queries, item.get("requested_url"), item.get("run_id"),
                    ),
                )
                return document_id, "new"
            extracted = bool(item.get("extracted"))
            was_extracted = bool(existing["extracted"])
            title, excerpt, digest = item["title"][:300], item["excerpt"][:2000], item["content_sha256"]
            if was_extracted and not extracted:
                # The fetch failed this time: keep the full-page copy rather than the snippet.
                title, excerpt, digest, extracted = existing["title"], existing["excerpt"], existing["content_sha256"], True
            changed = was_extracted == bool(item.get("extracted")) and existing["content_sha256"] != item["content_sha256"]
            status, reason = item["status"], item.get("status_reason")
            if existing["status"] == "kept" and status == "excluded" and reason == "outside_time_window":
                status, reason = existing["status"], existing["status_reason"]
            self.backend.execute(
                """
                UPDATE internet_documents SET url = ?, title = ?, excerpt = ?, content_sha256 = ?, published_at = COALESCE(?, published_at), date_status = ?, last_seen_at = ?, retrieved_at = ?,
                    match_level = ?, match_score = ?, match_reasons_json = ?, relevance_score = ?, topics_json = ?, status = ?, extracted = ?, warnings_json = ?,
                    status_reason = ?, provider = COALESCE(?, provider), queries_json = ?, requested_url = COALESCE(?, requested_url), last_run_id = COALESCE(?, last_run_id)
                WHERE institution_id = ? AND document_id = ?
                """,
                (
                    item["url"], title, excerpt, digest, item.get("published_at"), item.get("date_status", "unknown"), stamp, item.get("retrieved_at", stamp),
                    item["match_level"], float(item["match_score"]), _json(list(item.get("match_reasons", []))), float(item.get("relevance_score", 0.0)), _json(list(item.get("topics", []))), status,
                    1 if extracted else 0, _json(list(item.get("warnings", []))), reason, item.get("provider"), queries, item.get("requested_url"), item.get("run_id"), institution_id, existing["document_id"],
                ),
            )
            return existing["document_id"], "changed" if changed else "duplicate"

    def _document_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["match_reasons"] = _loads(row.pop("match_reasons_json", "[]"), [])
        row["topics"] = _loads(row.pop("topics_json", "[]"), [])
        row["warnings"] = _loads(row.pop("warnings_json", "[]"), [])
        row["extracted"] = bool(row.get("extracted"))
        row["queries"] = _loads(row.pop("queries_json", "[]"), [])
        return row

    def list_documents(self, institution_id: str, *, days: int | None = None, status: str | None = "kept", source_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["institution_id = ?"]
        params: list[Any] = [institution_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if days:
            clauses.append("last_seen_at >= ?")
            params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM internet_documents WHERE {' AND '.join(clauses)} ORDER BY COALESCE(published_at, last_seen_at) DESC LIMIT ?", (*params, max(1, min(limit, 1000))))
        return [self._document_row(row) for row in rows]

    def get_document(self, institution_id: str, document_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM internet_documents WHERE institution_id = ? AND document_id = ?", (institution_id, document_id))
        return self._document_row(row) if row else None

    # ------------------------------------------------------------- monitoring
    def start_run(self, institution_id: str, queries: Sequence[str]) -> str:
        run_id = f"mrun-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute("INSERT INTO monitoring_runs(run_id, institution_id, started_at, status, queries_json) VALUES (?, ?, ?, 'running', ?)", (run_id, institution_id, now_iso(), _json(list(queries))))
        return run_id

    def try_start_run(self, institution_id: str, queries: Sequence[str] = (), *, lock_seconds: int = RUN_LOCK_SECONDS) -> str | None:
        """Start a run unless one is already in progress for the institution; None when locked.

        A cron pass and a manual "run now" that overlap would otherwise both
        report the same items as new. A run left 'running' longer than
        ``lock_seconds`` lost its process: it is closed as abandoned and no
        longer blocks. The check and the insert are one statement.
        """

        run_id = f"mrun-{uuid4().hex}"
        started = datetime.now(timezone.utc)
        cutoff = (started - timedelta(seconds=lock_seconds)).isoformat()
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE monitoring_runs SET status = 'abandoned', finished_at = ?, error = 'no heartbeat: the run lost its process' WHERE institution_id = ? AND status = 'running' AND started_at < ?",
                (started.isoformat(), institution_id, cutoff),
            )
            inserted = self.backend.execute(
                "INSERT INTO monitoring_runs(run_id, institution_id, started_at, status, queries_json) SELECT CAST(? AS TEXT), CAST(? AS TEXT), CAST(? AS TEXT), 'running', CAST(? AS TEXT) WHERE NOT EXISTS (SELECT 1 FROM monitoring_runs WHERE institution_id = ? AND status = 'running')",
                (run_id, institution_id, started.isoformat(), _json(list(queries)), institution_id),
            )
        return run_id if inserted else None

    def run_count(self, institution_id: str) -> int:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT COUNT(*) AS runs FROM monitoring_runs WHERE institution_id = ?", (institution_id,))
        return int(row["runs"]) if row else 0

    def finish_run(
        self, institution_id: str, run_id: str, *, status: str, new_count: int, changed_count: int, duplicate_count: int, excluded_count: int, error: str | None = None,
        queries: Sequence[str] | None = None, stop_reason: str | None = None,
    ) -> None:
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE monitoring_runs SET finished_at = ?, status = ?, new_count = ?, changed_count = ?, duplicate_count = ?, excluded_count = ?, error = ?, queries_json = COALESCE(?, queries_json), stop_reason = COALESCE(?, stop_reason) WHERE institution_id = ? AND run_id = ?",
                (now_iso(), status, new_count, changed_count, duplicate_count, excluded_count, error, _json(list(queries)) if queries is not None else None, stop_reason, institution_id, run_id),
            )

    def add_event(self, institution_id: str, *, run_id: str, document_id: str, event_type: str, importance_level: str, source_type: str, title: str, url: str, summary: str) -> str:
        event_id = f"mevt-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO monitoring_events(event_id, institution_id, run_id, document_id, event_type, importance, source_type, title, url, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, institution_id, run_id, document_id, event_type, importance_level, source_type, title[:300], url, summary[:1000], now_iso()),
            )
        return event_id

    def mark_alerted(self, institution_id: str, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        with self._tenant(institution_id):
            self.backend.executemany("UPDATE monitoring_events SET alerted = 1 WHERE institution_id = ? AND event_id = ?", [(institution_id, event_id) for event_id in event_ids])

    def list_events(self, institution_id: str, *, days: int = 1, limit: int = 200) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        with self._tenant(institution_id):
            return self.backend.fetchall("SELECT * FROM monitoring_events WHERE institution_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT ?", (institution_id, since, max(1, min(limit, 1000))))

    def list_runs(self, institution_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT * FROM monitoring_runs WHERE institution_id = ? ORDER BY started_at DESC LIMIT ?", (institution_id, max(1, min(limit, 200))))
        for row in rows:
            row["queries"] = _loads(row.pop("queries_json", "[]"), [])
        return rows

    # ---------------------------------------------------------------- reports
    def save_report(self, institution_id: str, *, requested_by: str, question: str | None, window_days: int, summary: str, findings: Sequence[Mapping[str, Any]]) -> str:
        report_id = f"irpt-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO intelligence_reports(report_id, institution_id, requested_by, question, window_days, findings_count, summary, findings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, institution_id, requested_by, question, window_days, len(findings), summary[:4000], _json([dict(item) for item in findings]), now_iso()),
            )
        return report_id

    def list_reports(self, institution_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT report_id, requested_by, question, window_days, findings_count, summary, created_at FROM intelligence_reports WHERE institution_id = ? ORDER BY created_at DESC LIMIT ?", (institution_id, max(1, min(limit, 200))))
        return rows


__all__ = ["ADDED_COLUMNS", "RUN_LOCK_SECONDS", "IntelligenceStore", "SCHEMA_VERSION", "render_sql_migration"]
