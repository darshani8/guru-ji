from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.source_health import Freshness, SourceHealth, SourceHealthStatus


class InMemoryControlStore:
    """Small deterministic store used by unit tests and explicit ephemeral runs."""

    backend_name = "memory"

    def __init__(self, max_events: int = 1000) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: list[AuditEvent] = []
        self._max_events = max_events
        self._health: dict[str, SourceHealth] = {}
        self._briefings: list[dict[str, Any]] = []

    def append_audit(self, event: AuditEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max_events:
            del self._events[:-self._max_events]

    def recent_audit(self, limit: int = 100) -> tuple[AuditEvent, ...]:
        if limit <= 0:
            return ()
        return tuple(self._events[-limit:][::-1])

    def set_health(self, health: SourceHealth) -> None:
        self._health[health.source_id] = health

    def get_health(self, source_id: str) -> SourceHealth | None:
        return self._health.get(source_id)

    def all_health(self) -> tuple[SourceHealth, ...]:
        return tuple(self._health.values())

    def record_briefing(self, briefing_id: str, principal_id: str, institution_id: str, answer: dict[str, Any]) -> None:
        self._briefings.append({
            "briefing_id": briefing_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "principal_id": principal_id,
            "institution_id": institution_id,
            "answer": answer,
        })
        if len(self._briefings) > self._max_events:
            del self._briefings[:-self._max_events]

    def recent_briefings(self, limit: int = 50) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        return tuple(self._briefings[-limit:][::-1])

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class SqliteControlStore:
    """Durable local control-plane store.

    The institution data itself remains behind connector boundaries. This database
    stores only control-plane records: audit metadata, source health, and briefing
    envelopes. It is safe for local development and can be replaced by PostgreSQL
    behind the same interface for deployment.
    """

    backend_name = "sqlite"

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = self._path_from_url(database_url)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(":memory:" if self.path is None else str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    @staticmethod
    def _path_from_url(database_url: str) -> Path | None:
        if database_url == ":memory:" or database_url.endswith("/:memory:"):
            return None
        if not database_url.startswith("sqlite://"):
            raise ValueError("local control store requires a sqlite:// URL")
        raw = database_url.removeprefix("sqlite://")
        if raw.startswith("/"):
            raw = raw[1:]
        if raw.startswith("./"):
            return Path.cwd() / raw[2:]
        return Path(raw).expanduser().resolve()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                request_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                principal_id TEXT,
                endpoint TEXT,
                conversation_id TEXT,
                source_ids_json TEXT NOT NULL,
                tool_names_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                redactions_json TEXT NOT NULL,
                decision_metadata_json TEXT NOT NULL DEFAULT '{}',
                duration_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at
                ON audit_events(occurred_at DESC);
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                connector_type TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                last_success_at TEXT,
                latency_ms INTEGER,
                freshness TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS briefing_runs (
                briefing_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                answer_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_briefing_runs_created_at
                ON briefing_runs(created_at DESC);
            """
        )
        for column, definition in (
            ("institution_id", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("connector_type", "TEXT NOT NULL DEFAULT ''"),
            ("last_success_at", "TEXT"),
        ):
            try:
                self._connection.execute(f"ALTER TABLE source_health ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        try:
            self._connection.execute(
                "ALTER TABLE audit_events ADD COLUMN decision_metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("001_control_plane", datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, event_type, request_id, occurred_at, principal_id,
                    endpoint, conversation_id, source_ids_json, tool_names_json,
                    outcome, redactions_json, decision_metadata_json, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.request_id,
                    event.occurred_at.isoformat(),
                    event.principal_id,
                    event.endpoint,
                    event.conversation_id,
                    self._json(event.source_ids),
                    self._json(event.tool_names),
                    event.outcome.value,
                    self._json(event.redactions_applied),
                    self._json(dict(event.decision_metadata)),
                    event.duration_ms,
                ),
            )
            self._connection.commit()

    def recent_audit(self, limit: int = 100) -> tuple[AuditEvent, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_events ORDER BY occurred_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return tuple(
            AuditEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                request_id=row["request_id"],
                occurred_at=self._parse_datetime(row["occurred_at"]),
                principal_id=row["principal_id"],
                endpoint=row["endpoint"],
                conversation_id=row["conversation_id"],
                source_ids=tuple(json.loads(row["source_ids_json"])),
                tool_names=tuple(json.loads(row["tool_names_json"])),
                outcome=AuditOutcome(row["outcome"]),
                redactions_applied=tuple(json.loads(row["redactions_json"])),
                decision_metadata=tuple(json.loads(row["decision_metadata_json"]).items()),
                duration_ms=row["duration_ms"],
            )
            for row in rows
        )

    def set_health(self, health: SourceHealth) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO source_health(
                    source_id, institution_id, status, display_name, connector_type,
                    checked_at, last_success_at, latency_ms, freshness, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    health.source_id,
                    health.institution_id,
                    health.status.value,
                    health.display_name,
                    health.connector_type,
                    health.checked_at.isoformat(),
                    health.last_success_at.isoformat() if health.last_success_at else None,
                    health.latency_ms,
                    health.freshness.value,
                    health.detail,
                ),
            )
            self._connection.commit()

    def _health_from_row(self, row: sqlite3.Row) -> SourceHealth:
        return SourceHealth(
            source_id=row["source_id"],
            institution_id=row["institution_id"],
            status=SourceHealthStatus(row["status"]),
            display_name=row["display_name"],
            connector_type=row["connector_type"],
            checked_at=self._parse_datetime(row["checked_at"]),
            last_success_at=self._parse_datetime(row["last_success_at"]) if row["last_success_at"] else None,
            latency_ms=row["latency_ms"],
            freshness=Freshness(row["freshness"]),
            detail=row["detail"],
        )

    def get_health(self, source_id: str) -> SourceHealth | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM source_health WHERE source_id = ?", (source_id,)
            ).fetchone()
        return self._health_from_row(row) if row else None

    def all_health(self) -> tuple[SourceHealth, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM source_health ORDER BY source_id"
            ).fetchall()
        return tuple(self._health_from_row(row) for row in rows)

    def record_briefing(self, briefing_id: str, principal_id: str, institution_id: str, answer: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO briefing_runs(
                    briefing_id, created_at, principal_id, institution_id, answer_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    briefing_id,
                    datetime.now(timezone.utc).isoformat(),
                    principal_id,
                    institution_id,
                    self._json(answer),
                ),
            )
            self._connection.commit()

    def recent_briefings(self, limit: int = 50) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM briefing_runs ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return tuple(
            {
                "briefing_id": row["briefing_id"],
                "created_at": row["created_at"],
                "principal_id": row["principal_id"],
                "institution_id": row["institution_id"],
                "answer": json.loads(row["answer_json"]),
            }
            for row in rows
        )

    def ping(self) -> bool:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True


class PostgresControlStore:
    """Durable PostgreSQL control-plane store for deployment environments.

    Only application control-plane metadata is persisted here. Institutional data
    remains behind connector boundaries and is never copied into this database.
    The synchronous interface intentionally matches the local stores so the
    orchestration and route layers cannot accidentally bypass the same policy.
    """

    backend_name = "postgresql"

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgresControlStore requires a postgresql:// or postgres:// URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised in dependency-free installs
            raise RuntimeError("PostgreSQL support requires the psycopg[binary] dependency") from exc

        self.database_url = database_url
        self._lock = threading.RLock()
        self._connection = psycopg.connect(database_url, row_factory=dict_row)
        self._closed = False
        self._migrate()

    def _migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                request_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                principal_id TEXT,
                endpoint TEXT,
                conversation_id TEXT,
                source_ids_json TEXT NOT NULL,
                tool_names_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                redactions_json TEXT NOT NULL,
                decision_metadata_json TEXT NOT NULL DEFAULT '{}',
                duration_ms INTEGER
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at ON audit_events(occurred_at DESC)",
            "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS decision_metadata_json TEXT NOT NULL DEFAULT '{}'",
            """
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                connector_type TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                last_success_at TEXT,
                latency_ms INTEGER,
                freshness TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """,
            "ALTER TABLE source_health ADD COLUMN IF NOT EXISTS institution_id TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE source_health ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE source_health ADD COLUMN IF NOT EXISTS connector_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_success_at TEXT",
            """
            CREATE TABLE IF NOT EXISTS briefing_runs (
                briefing_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                answer_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_briefing_runs_created_at ON briefing_runs(created_at DESC)",
        )
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)
                    cursor.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                        ("001_control_plane", datetime.now(timezone.utc).isoformat()),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit_events(
                        event_id, event_type, request_id, occurred_at, principal_id,
                        endpoint, conversation_id, source_ids_json, tool_names_json,
                        outcome, redactions_json, decision_metadata_json, duration_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.request_id,
                        event.occurred_at.isoformat(),
                        event.principal_id,
                        event.endpoint,
                        event.conversation_id,
                        self._json(event.source_ids),
                        self._json(event.tool_names),
                        event.outcome.value,
                        self._json(event.redactions_applied),
                        self._json(dict(event.decision_metadata)),
                        event.duration_ms,
                    ),
                )
            self._connection.commit()

    def recent_audit(self, limit: int = 100) -> tuple[AuditEvent, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT * FROM audit_events ORDER BY occurred_at DESC LIMIT %s", (int(limit),))
                rows = cursor.fetchall()
        return tuple(
            AuditEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                request_id=row["request_id"],
                occurred_at=self._parse_datetime(row["occurred_at"]),
                principal_id=row["principal_id"],
                endpoint=row["endpoint"],
                conversation_id=row["conversation_id"],
                source_ids=tuple(json.loads(row["source_ids_json"])),
                tool_names=tuple(json.loads(row["tool_names_json"])),
                outcome=AuditOutcome(row["outcome"]),
                redactions_applied=tuple(json.loads(row["redactions_json"])),
                decision_metadata=tuple(json.loads(row["decision_metadata_json"]).items()),
                duration_ms=row["duration_ms"],
            )
            for row in rows
        )

    def set_health(self, health: SourceHealth) -> None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO source_health(
                        source_id, institution_id, status, display_name, connector_type,
                        checked_at, last_success_at, latency_ms, freshness, detail
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id) DO UPDATE SET
                        institution_id = EXCLUDED.institution_id,
                        status = EXCLUDED.status,
                        display_name = EXCLUDED.display_name,
                        connector_type = EXCLUDED.connector_type,
                        checked_at = EXCLUDED.checked_at,
                        last_success_at = EXCLUDED.last_success_at,
                        latency_ms = EXCLUDED.latency_ms,
                        freshness = EXCLUDED.freshness,
                        detail = EXCLUDED.detail
                    """,
                    (
                        health.source_id,
                        health.institution_id,
                        health.status.value,
                        health.display_name,
                        health.connector_type,
                        health.checked_at.isoformat(),
                        health.last_success_at.isoformat() if health.last_success_at else None,
                        health.latency_ms,
                        health.freshness.value,
                        health.detail,
                    ),
                )
            self._connection.commit()

    @staticmethod
    def _health_from_row(row: dict[str, Any]) -> SourceHealth:
        return SourceHealth(
            source_id=row["source_id"],
            institution_id=row["institution_id"],
            status=SourceHealthStatus(row["status"]),
            display_name=row["display_name"],
            connector_type=row["connector_type"],
            checked_at=PostgresControlStore._parse_datetime(row["checked_at"]),
            last_success_at=PostgresControlStore._parse_datetime(row["last_success_at"]) if row["last_success_at"] else None,
            latency_ms=row["latency_ms"],
            freshness=Freshness(row["freshness"]),
            detail=row["detail"],
        )

    def get_health(self, source_id: str) -> SourceHealth | None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT * FROM source_health WHERE source_id = %s", (source_id,))
                row = cursor.fetchone()
        return self._health_from_row(row) if row else None

    def all_health(self) -> tuple[SourceHealth, ...]:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT * FROM source_health ORDER BY source_id")
                rows = cursor.fetchall()
        return tuple(self._health_from_row(row) for row in rows)

    def record_briefing(self, briefing_id: str, principal_id: str, institution_id: str, answer: dict[str, Any]) -> None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO briefing_runs(briefing_id, created_at, principal_id, institution_id, answer_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (briefing_id) DO UPDATE SET
                        created_at = EXCLUDED.created_at,
                        principal_id = EXCLUDED.principal_id,
                        institution_id = EXCLUDED.institution_id,
                        answer_json = EXCLUDED.answer_json
                    """,
                    (
                        briefing_id,
                        datetime.now(timezone.utc).isoformat(),
                        principal_id,
                        institution_id,
                        self._json(answer),
                    ),
                )
            self._connection.commit()

    def recent_briefings(self, limit: int = 50) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT * FROM briefing_runs ORDER BY created_at DESC LIMIT %s", (int(limit),))
                rows = cursor.fetchall()
        return tuple(
            {
                "briefing_id": row["briefing_id"],
                "created_at": row["created_at"],
                "principal_id": row["principal_id"],
                "institution_id": row["institution_id"],
                "answer": json.loads(row["answer_json"]),
            }
            for row in rows
        )

    def ping(self) -> bool:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True


__all__ = ["InMemoryControlStore", "PostgresControlStore", "SqliteControlStore"]
