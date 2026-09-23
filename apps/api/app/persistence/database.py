from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.source_health import Freshness, SourceHealth, SourceHealthStatus
from .control_plane import AnswerEnvelopeMetadata, ModelAttemptMetadata, OutboxRecord, now_utc
from .schema_tools import MIGRATION_LOCK_TIMEOUT
from .sql_backend import open_postgres_connection

# Voice sessions and usage counters are short-lived control records: a closed
# or expired session row is kept for a day (enough to answer a late DELETE or a
# replayed ticket), a usage counter for 30 days.
VOICE_SESSION_KEEP = timedelta(days=1)
USAGE_COUNTER_KEEP_DAYS = 30
_VOICE_SESSION_COLUMNS = (
    "session_id", "principal_id", "institution_id", "principal_json", "ticket_sha256",
    "created_at", "ticket_expires_at", "expires_at", "claimed_at", "closed_at",
)


def timestamp(value: datetime) -> str:
    """One fixed ISO-8601 UTC form, so stored timestamps compare correctly as text."""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _voice_row(row: Any) -> dict[str, Any]:
    return {column: row[column] for column in _VOICE_SESSION_COLUMNS}


def _is_active(row: dict[str, Any], now: str) -> bool:
    """Open, and either waiting for its socket (ticket still valid) or connected (session not expired)."""

    if row["closed_at"] is not None:
        return False
    if row["claimed_at"] is None:
        return row["ticket_expires_at"] > now and row["expires_at"] > now
    return row["expires_at"] > now


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
        self._answers: list[AnswerEnvelopeMetadata] = []
        self._attempts: list[ModelAttemptMetadata] = []
        self._outbox: list[OutboxRecord] = []
        self._voice_sessions: dict[str, dict[str, Any]] = {}
        self._usage: dict[tuple[str, str, str, str], int] = {}
        self._records_lock = threading.Lock()

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

    def record_answer_envelope(self, envelope: AnswerEnvelopeMetadata) -> None:
        self._answers = [item for item in self._answers if item.request_id != envelope.request_id]
        self._answers.append(envelope)

    def record_model_attempt(self, attempt: ModelAttemptMetadata) -> None:
        self._attempts.append(attempt)
        if len(self._attempts) > self._max_events:
            del self._attempts[:-self._max_events]

    def enqueue_outbox(self, event_type: str, payload: dict[str, Any]) -> str:
        outbox_id = f"outbox-{uuid4().hex}"
        self._outbox.append(OutboxRecord(outbox_id, event_type, dict(payload), now_utc()))
        return outbox_id

    def drain_outbox(self, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if limit <= 0:
            return ()
        pending = [item for item in self._outbox if item.delivered_at is None][:limit]
        return tuple(pending)

    def ack_outbox(self, outbox_ids: tuple[str, ...]) -> None:
        acknowledged = set(outbox_ids)
        if not acknowledged:
            return
        self._outbox = [
            item for item in self._outbox
            if item.outbox_id not in acknowledged
        ]

    def prune_retention(self, retention_days: int) -> int:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        cutoff = now_utc() - timedelta(days=retention_days)
        before = len(self._events) + len(self._answers) + len(self._attempts) + len(self._outbox) + len(self._briefings)
        self._events = [item for item in self._events if item.occurred_at >= cutoff]
        self._answers = [item for item in self._answers if item.created_at >= cutoff]
        self._attempts = [item for item in self._attempts if item.created_at >= cutoff]
        self._outbox = [item for item in self._outbox if item.created_at >= cutoff]
        self._briefings = [item for item in self._briefings if datetime.fromisoformat(item["created_at"]) >= cutoff]
        return before - (len(self._events) + len(self._answers) + len(self._attempts) + len(self._outbox) + len(self._briefings))

    # ---------------------------------------------------- voice sessions
    def create_voice_session(self, record: dict[str, Any]) -> None:
        with self._records_lock:
            if record["session_id"] in self._voice_sessions:
                raise ValueError("voice session already exists")
            self._voice_sessions[record["session_id"]] = {column: record.get(column) for column in _VOICE_SESSION_COLUMNS}

    def get_voice_session(self, session_id: str) -> dict[str, Any] | None:
        with self._records_lock:
            row = self._voice_sessions.get(session_id)
            return dict(row) if row else None

    def claim_voice_session(self, session_id: str, ticket_sha256: str, now: str) -> dict[str, Any] | None:
        with self._records_lock:
            row = self._voice_sessions.get(session_id)
            if (
                row is None or row["ticket_sha256"] != ticket_sha256 or row["claimed_at"] is not None
                or row["closed_at"] is not None or row["ticket_expires_at"] <= now or row["expires_at"] <= now
            ):
                return None
            row["claimed_at"] = now
            return dict(row)

    def close_voice_session(self, session_id: str, principal_id: str | None, now: str) -> bool:
        with self._records_lock:
            row = self._voice_sessions.get(session_id)
            if row is None or row["closed_at"] is not None or (principal_id is not None and row["principal_id"] != principal_id):
                return False
            row["closed_at"] = now
            return True

    def active_voice_sessions(self, now: str, principal_id: str | None = None) -> list[dict[str, Any]]:
        with self._records_lock:
            rows = [dict(row) for row in self._voice_sessions.values() if _is_active(row, now) and (principal_id is None or row["principal_id"] == principal_id)]
        return sorted(rows, key=lambda row: row["created_at"])

    def take_usage(self, *, institution_id: str, principal_id: str, counter: str, day: str, cap: int) -> bool:
        """Count one use against today's cap; False (and nothing counted) once the cap is reached."""

        key = (institution_id, principal_id, counter, day)
        with self._records_lock:
            used = self._usage.get(key, 0)
            if used >= cap:
                return False
            self._usage[key] = used + 1
            return True

    def prune_ephemeral(self, now: datetime | None = None) -> int:
        current = now or now_utc()
        cutoff = timestamp(current - VOICE_SESSION_KEEP)
        oldest_day = (current - timedelta(days=USAGE_COUNTER_KEEP_DAYS)).date().isoformat()
        with self._records_lock:
            stale = [key for key, row in self._voice_sessions.items() if (row["closed_at"] or row["expires_at"]) < cutoff]
            for key in stale:
                del self._voice_sessions[key]
            old = [key for key in self._usage if key[3] < oldest_day]
            for key in old:
                del self._usage[key]
        return len(stale) + len(old)

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
            CREATE TABLE IF NOT EXISTS answer_envelopes (
                request_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                principal_id TEXT,
                college_id TEXT,
                status TEXT NOT NULL,
                citations_count INTEGER NOT NULL,
                warnings_count INTEGER NOT NULL,
                answer_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_answer_envelopes_created_at
                ON answer_envelopes(created_at DESC);
            CREATE TABLE IF NOT EXISTS model_attempts (
                attempt_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                fallback INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cost REAL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_attempts_created_at
                ON model_attempts(created_at DESC);
            CREATE TABLE IF NOT EXISTS control_outbox (
                outbox_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_control_outbox_pending
                ON control_outbox(delivered_at, created_at);
            CREATE TABLE IF NOT EXISTS voice_sessions (
                session_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                principal_json TEXT NOT NULL,
                ticket_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ticket_expires_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                claimed_at TEXT,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_principal
                ON voice_sessions(principal_id, closed_at);
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_expires_at
                ON voice_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS usage_counters (
                institution_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                counter TEXT NOT NULL,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (institution_id, principal_id, counter, day)
            );
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

        for version in ("001_control_plane", "005_voice_conversation"):
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
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

    def record_answer_envelope(self, envelope: AnswerEnvelopeMetadata) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO answer_envelopes(
                    request_id, conversation_id, principal_id, college_id, status,
                    citations_count, warnings_count, answer_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.request_id, envelope.conversation_id, envelope.principal_id,
                    envelope.college_id, envelope.status, envelope.citations_count,
                    envelope.warnings_count, envelope.answer_sha256, envelope.created_at.isoformat(),
                ),
            )
            self._connection.commit()

    def record_model_attempt(self, attempt: ModelAttemptMetadata) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO model_attempts(
                    attempt_id, request_id, provider_id, model_id, outcome, latency_ms,
                    fallback, prompt_tokens, completion_tokens, total_tokens, cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt-{uuid4().hex}", attempt.request_id, attempt.provider_id,
                    attempt.model_id, attempt.outcome, attempt.latency_ms, int(attempt.fallback),
                    attempt.prompt_tokens, attempt.completion_tokens, attempt.total_tokens,
                    attempt.cost, attempt.created_at.isoformat(),
                ),
            )
            self._connection.commit()

    def enqueue_outbox(self, event_type: str, payload: dict[str, Any]) -> str:
        outbox_id = f"outbox-{uuid4().hex}"
        with self._lock:
            self._connection.execute(
                "INSERT INTO control_outbox(outbox_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (outbox_id, event_type, self._json(payload), now_utc().isoformat()),
            )
            self._connection.commit()
        return outbox_id

    def drain_outbox(self, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM control_outbox WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?", (int(limit),)
            ).fetchall()
        return tuple(OutboxRecord(
            outbox_id=row["outbox_id"], event_type=row["event_type"],
            payload=json.loads(row["payload_json"]), created_at=self._parse_datetime(row["created_at"]),
            delivered_at=self._parse_datetime(row["delivered_at"]) if row["delivered_at"] else None,
        ) for row in rows)

    def ack_outbox(self, outbox_ids: tuple[str, ...]) -> None:
        if not outbox_ids:
            return
        delivered_at = now_utc().isoformat()
        with self._lock:
            self._connection.executemany(
                "UPDATE control_outbox SET delivered_at = ? WHERE outbox_id = ? AND delivered_at IS NULL",
                [(delivered_at, outbox_id) for outbox_id in outbox_ids],
            )
            self._connection.commit()

    def prune_retention(self, retention_days: int) -> int:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        cutoff = (now_utc() - timedelta(days=retention_days)).isoformat()
        with self._lock:
            total = 0
            for table in ("audit_events", "answer_envelopes", "model_attempts", "control_outbox", "briefing_runs"):
                column = "occurred_at" if table == "audit_events" else "created_at"
                cursor = self._connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                total += cursor.rowcount
            self._connection.commit()
        return total

    # ---------------------------------------------------- voice sessions
    def create_voice_session(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                f"INSERT INTO voice_sessions({', '.join(_VOICE_SESSION_COLUMNS)}) VALUES ({', '.join('?' for _ in _VOICE_SESSION_COLUMNS)})",
                tuple(record.get(column) for column in _VOICE_SESSION_COLUMNS),
            )
            self._connection.commit()

    def get_voice_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM voice_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _voice_row(row) if row else None

    def claim_voice_session(self, session_id: str, ticket_sha256: str, now: str) -> dict[str, Any] | None:
        # One conditional UPDATE: of two sockets presenting the same ticket, on
        # any number of API tasks sharing this database, exactly one wins.
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE voice_sessions SET claimed_at = ? WHERE session_id = ? AND ticket_sha256 = ? AND claimed_at IS NULL "
                "AND closed_at IS NULL AND ticket_expires_at > ? AND expires_at > ?",
                (now, session_id, ticket_sha256, now, now),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                return None
            row = self._connection.execute("SELECT * FROM voice_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _voice_row(row) if row else None

    def close_voice_session(self, session_id: str, principal_id: str | None, now: str) -> bool:
        with self._lock:
            if principal_id is None:
                cursor = self._connection.execute("UPDATE voice_sessions SET closed_at = ? WHERE session_id = ? AND closed_at IS NULL", (now, session_id))
            else:
                cursor = self._connection.execute(
                    "UPDATE voice_sessions SET closed_at = ? WHERE session_id = ? AND principal_id = ? AND closed_at IS NULL", (now, session_id, principal_id),
                )
            self._connection.commit()
        return cursor.rowcount == 1

    def active_voice_sessions(self, now: str, principal_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM voice_sessions WHERE closed_at IS NULL AND expires_at > ? "
            "AND (claimed_at IS NOT NULL OR ticket_expires_at > ?)"
        )
        params: tuple[Any, ...] = (now, now)
        if principal_id is not None:
            query += " AND principal_id = ?"
            params += (principal_id,)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [_voice_row(row) for row in rows]

    def take_usage(self, *, institution_id: str, principal_id: str, counter: str, day: str, cap: int) -> bool:
        """Count one use against today's cap; False (and nothing counted) once the cap is reached."""

        if cap <= 0:
            return False
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO usage_counters(institution_id, principal_id, counter, day, used) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT (institution_id, principal_id, counter, day) DO UPDATE SET used = usage_counters.used + 1 WHERE usage_counters.used < ?",
                (institution_id, principal_id, counter, day, cap),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def prune_ephemeral(self, now: datetime | None = None) -> int:
        current = now or now_utc()
        cutoff = timestamp(current - VOICE_SESSION_KEEP)
        oldest_day = (current - timedelta(days=USAGE_COUNTER_KEEP_DAYS)).date().isoformat()
        with self._lock:
            sessions = self._connection.execute("DELETE FROM voice_sessions WHERE COALESCE(closed_at, expires_at) < ?", (cutoff,)).rowcount
            counters = self._connection.execute("DELETE FROM usage_counters WHERE day < ?", (oldest_day,)).rowcount
            self._connection.commit()
        return sessions + counters

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
        # Autocommit: a statement outside an explicit ``transaction()`` block
        # is its own transaction, so a read never leaves the connection idle
        # in transaction holding table locks. Before this, a running instance
        # blocked the next release's start-up DDL until the deploy timed out.
        self._connection = open_postgres_connection(psycopg, database_url, row_factory=dict_row, autocommit=True)
        self._closed = False
        self._migrate()

    # How long start-up may wait for a table lock before failing loudly. A
    # hang here is invisible (no log line, health check never answers), an
    # error is not.
    MIGRATION_LOCK_TIMEOUT = MIGRATION_LOCK_TIMEOUT

    # Columns added after their table first shipped and the indexes, each run
    # only when the catalog shows it missing: ``ADD COLUMN IF NOT EXISTS`` and
    # ``CREATE INDEX IF NOT EXISTS`` still take a table lock even when they
    # end up doing nothing, and the previous release is still serving.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("audit_events", "decision_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("source_health", "institution_id", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("source_health", "display_name", "TEXT NOT NULL DEFAULT ''"),
        ("source_health", "connector_type", "TEXT NOT NULL DEFAULT ''"),
        ("source_health", "last_success_at", "TEXT"),
    )
    _INDEXES: tuple[tuple[str, str], ...] = (
        ("idx_audit_events_occurred_at", "audit_events(occurred_at DESC)"),
        ("idx_briefing_runs_created_at", "briefing_runs(created_at DESC)"),
        ("idx_answer_envelopes_created_at", "answer_envelopes(created_at DESC)"),
        ("idx_model_attempts_created_at", "model_attempts(created_at DESC)"),
        ("idx_control_outbox_pending", "control_outbox(delivered_at, created_at)"),
        ("idx_voice_sessions_principal", "voice_sessions(principal_id, closed_at)"),
        ("idx_voice_sessions_expires_at", "voice_sessions(expires_at)"),
    )

    def _migrate(self) -> None:
        """Bring the schema up to date, issuing DDL only for what is missing.

        Another instance of the API is normally still serving while a new one
        starts, so start-up must not queue behind its connections for an
        ACCESS EXCLUSIVE lock. ``CREATE TABLE IF NOT EXISTS`` skips an existing
        table without locking it; columns and indexes are checked in the
        catalog first; and ``lock_timeout`` turns any wait that does happen
        into an error instead of a silent hang.
        """

        tables = (
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
            """
            CREATE TABLE IF NOT EXISTS briefing_runs (
                briefing_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                answer_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS answer_envelopes (
                request_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                principal_id TEXT,
                college_id TEXT,
                status TEXT NOT NULL,
                citations_count INTEGER NOT NULL,
                warnings_count INTEGER NOT NULL,
                answer_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_attempts (
                attempt_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                fallback BOOLEAN NOT NULL DEFAULT FALSE,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cost DOUBLE PRECISION,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS control_outbox (
                outbox_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS voice_sessions (
                session_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                principal_json TEXT NOT NULL,
                ticket_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ticket_expires_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                claimed_at TEXT,
                closed_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS usage_counters (
                institution_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                counter TEXT NOT NULL,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (institution_id, principal_id, counter, day)
            )
            """,
        )
        with self._lock, self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{self.MIGRATION_LOCK_TIMEOUT}'")
            for statement in tables:
                cursor.execute(statement)
            for table, column, definition in self._ADDED_COLUMNS:
                cursor.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                    (table, column),
                )
                if cursor.fetchone() is None:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            for index, definition in self._INDEXES:
                cursor.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s", (index,))
                if cursor.fetchone() is None:
                    cursor.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {definition}")
            for version in ("001_control_plane", "005_voice_conversation"):
                cursor.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                    (version, datetime.now(timezone.utc).isoformat()),
                )

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

    def record_answer_envelope(self, envelope: AnswerEnvelopeMetadata) -> None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO answer_envelopes(
                        request_id, conversation_id, principal_id, college_id, status,
                        citations_count, warnings_count, answer_sha256, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (request_id) DO UPDATE SET
                        conversation_id = EXCLUDED.conversation_id,
                        principal_id = EXCLUDED.principal_id,
                        college_id = EXCLUDED.college_id,
                        status = EXCLUDED.status,
                        citations_count = EXCLUDED.citations_count,
                        warnings_count = EXCLUDED.warnings_count,
                        answer_sha256 = EXCLUDED.answer_sha256,
                        created_at = EXCLUDED.created_at
                    """,
                    (
                        envelope.request_id, envelope.conversation_id, envelope.principal_id,
                        envelope.college_id, envelope.status, envelope.citations_count,
                        envelope.warnings_count, envelope.answer_sha256, envelope.created_at.isoformat(),
                    ),
                )

    def record_model_attempt(self, attempt: ModelAttemptMetadata) -> None:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO model_attempts(
                        attempt_id, request_id, provider_id, model_id, outcome, latency_ms,
                        fallback, prompt_tokens, completion_tokens, total_tokens, cost, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"attempt-{uuid4().hex}", attempt.request_id, attempt.provider_id,
                        attempt.model_id, attempt.outcome, attempt.latency_ms, attempt.fallback,
                        attempt.prompt_tokens, attempt.completion_tokens, attempt.total_tokens,
                        attempt.cost, attempt.created_at.isoformat(),
                    ),
                )

    def enqueue_outbox(self, event_type: str, payload: dict[str, Any]) -> str:
        outbox_id = f"outbox-{uuid4().hex}"
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO control_outbox(outbox_id, event_type, payload_json, created_at) VALUES (%s, %s, %s, %s)",
                    (outbox_id, event_type, self._json(payload), now_utc().isoformat()),
                )
        return outbox_id

    def drain_outbox(self, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM control_outbox WHERE delivered_at IS NULL ORDER BY created_at LIMIT %s",
                    (int(limit),),
                )
                rows = cursor.fetchall()
        return tuple(OutboxRecord(
            outbox_id=row["outbox_id"], event_type=row["event_type"],
            payload=json.loads(row["payload_json"]), created_at=self._parse_datetime(row["created_at"]),
            delivered_at=self._parse_datetime(row["delivered_at"]) if row["delivered_at"] else None,
        ) for row in rows)

    def ack_outbox(self, outbox_ids: tuple[str, ...]) -> None:
        if not outbox_ids:
            return
        delivered_at = now_utc().isoformat()
        with self._lock, self._connection.transaction(), self._connection.cursor() as cursor:
            for outbox_id in outbox_ids:
                cursor.execute(
                    "UPDATE control_outbox SET delivered_at = %s WHERE outbox_id = %s AND delivered_at IS NULL",
                    (delivered_at, outbox_id),
                )

    def prune_retention(self, retention_days: int) -> int:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        cutoff = (now_utc() - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connection.transaction(), self._connection.cursor() as cursor:
            # Pruning runs at start-up too: a lock it cannot get promptly (a
            # queued exclusive request from a dead client) must fail, not hang.
            cursor.execute(f"SET LOCAL lock_timeout = '{self.MIGRATION_LOCK_TIMEOUT}'")
            total = 0
            for table in ("audit_events", "answer_envelopes", "model_attempts", "control_outbox", "briefing_runs"):
                column = "occurred_at" if table == "audit_events" else "created_at"
                cursor.execute(f"DELETE FROM {table} WHERE {column} < %s", (cutoff,))
                total += cursor.rowcount
        return total

    # ---------------------------------------------------- voice sessions
    def create_voice_session(self, record: dict[str, Any]) -> None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO voice_sessions({', '.join(_VOICE_SESSION_COLUMNS)}) VALUES ({', '.join('%s' for _ in _VOICE_SESSION_COLUMNS)})",
                tuple(record.get(column) for column in _VOICE_SESSION_COLUMNS),
            )

    def get_voice_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM voice_sessions WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
        return _voice_row(row) if row else None

    def claim_voice_session(self, session_id: str, ticket_sha256: str, now: str) -> dict[str, Any] | None:
        # One conditional UPDATE: of two sockets presenting the same ticket, on
        # any number of API tasks sharing this database, exactly one wins.
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE voice_sessions SET claimed_at = %s WHERE session_id = %s AND ticket_sha256 = %s AND claimed_at IS NULL "
                "AND closed_at IS NULL AND ticket_expires_at > %s AND expires_at > %s RETURNING *",
                (now, session_id, ticket_sha256, now, now),
            )
            row = cursor.fetchone()
        return _voice_row(row) if row else None

    def close_voice_session(self, session_id: str, principal_id: str | None, now: str) -> bool:
        with self._lock, self._connection.cursor() as cursor:
            if principal_id is None:
                cursor.execute("UPDATE voice_sessions SET closed_at = %s WHERE session_id = %s AND closed_at IS NULL", (now, session_id))
            else:
                cursor.execute(
                    "UPDATE voice_sessions SET closed_at = %s WHERE session_id = %s AND principal_id = %s AND closed_at IS NULL", (now, session_id, principal_id),
                )
            return cursor.rowcount == 1

    def active_voice_sessions(self, now: str, principal_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM voice_sessions WHERE closed_at IS NULL AND expires_at > %s "
            "AND (claimed_at IS NOT NULL OR ticket_expires_at > %s)"
        )
        params: tuple[Any, ...] = (now, now)
        if principal_id is not None:
            query += " AND principal_id = %s"
            params += (principal_id,)
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query + " ORDER BY created_at", params)
            rows = cursor.fetchall()
        return [_voice_row(row) for row in rows]

    def take_usage(self, *, institution_id: str, principal_id: str, counter: str, day: str, cap: int) -> bool:
        """Count one use against today's cap; False (and nothing counted) once the cap is reached."""

        if cap <= 0:
            return False
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO usage_counters(institution_id, principal_id, counter, day, used) VALUES (%s, %s, %s, %s, 1) "
                "ON CONFLICT (institution_id, principal_id, counter, day) DO UPDATE SET used = usage_counters.used + 1 WHERE usage_counters.used < %s",
                (institution_id, principal_id, counter, day, cap),
            )
            return cursor.rowcount == 1

    def prune_ephemeral(self, now: datetime | None = None) -> int:
        current = now or now_utc()
        cutoff = timestamp(current - VOICE_SESSION_KEEP)
        oldest_day = (current - timedelta(days=USAGE_COUNTER_KEEP_DAYS)).date().isoformat()
        with self._lock, self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{self.MIGRATION_LOCK_TIMEOUT}'")
            cursor.execute("DELETE FROM voice_sessions WHERE COALESCE(closed_at, expires_at) < %s", (cutoff,))
            total = cursor.rowcount
            cursor.execute("DELETE FROM usage_counters WHERE day < %s", (oldest_day,))
            total += cursor.rowcount
        return total

    def ping(self) -> bool:
        with self._lock:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return True

    @property
    def in_transaction(self) -> bool:
        """True while the connection holds a transaction open (never between calls)."""

        with self._lock:
            # libpq statuses: 0 IDLE, 1 ACTIVE, 2 INTRANS, 3 INERROR, 4 UNKNOWN (closed).
            return int(getattr(self._connection.info, "transaction_status", 0)) in (1, 2, 3)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True


__all__ = ["InMemoryControlStore", "PostgresControlStore", "SqliteControlStore", "timestamp"]
