"""The institution data store: the only code that touches canonical tables.

Every method takes the ``institution_id`` it serves and adds it to the SQL it
runs, so a caller cannot reach another tenant's rows even before PostgreSQL
row-level security is considered. No method accepts SQL from a caller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..normalization.canonical import CANONICAL_ENTITIES, CanonicalEntity, CanonicalField, FieldType, entity as canonical_entity
from ..persistence.sql_backend import SqlBackend, open_backend
from .models import CanonicalRecord, ImportSummary
from ..persistence.schema_tools import add_missing_columns, apply_schema, begin_migration, existing_policies, row_level_security_state, tenant_isolation_statements
from .schema import ADDED_COLUMNS, SCHEMA_VERSION, TENANT_TABLES, portable_statements, postgres_numeric_columns

MAX_QUERY_ROWS = 5_000
# Rows written per transaction by bulk imports; the backend lock is released
# between chunks so request handlers are never blocked for a whole import.
WRITE_CHUNK_ROWS = 500
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _clamp_limit(limit: int, maximum: int = MAX_QUERY_ROWS) -> int:
    if limit <= 0:
        return 1
    return min(int(limit), maximum)


def _column(name: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe column name: {name!r}")
    return name


def _bool_param(item: CanonicalField, value: Any) -> Any:
    """Bind BOOLEAN fields as the 1/0 integers they are stored as.

    psycopg binds a Python ``bool`` with the boolean OID, which PostgreSQL
    refuses to compare with an INTEGER column, so filters must convert exactly
    like ``upsert_records`` and ``update_record_fields`` do on write.
    """

    if item.field_type is not FieldType.BOOLEAN:
        return value
    if isinstance(value, (bool, int)):
        return 1 if value else 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "t"}:
            return 1
        if text in {"0", "false", "no", "n", "f"}:
            return 0
    return value


def _chunks(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for element in items:
        batch.append(element)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class InstitutionDataStore:
    backend_name: str

    def __init__(self, database_url: str | None = None, backend: SqlBackend | None = None) -> None:
        self.backend = backend or open_backend(database_url)
        self.backend_name = self.backend.dialect
        self._migrate()

    # ------------------------------------------------------------------ lifecycle
    def _migrate(self) -> None:
        """Bring the schema up to date in one transaction, issuing DDL only for what is missing.

        The previous release is normally still serving while a new one starts,
        so on PostgreSQL nothing here may queue for a lock behind its
        statements: ``CREATE TABLE IF NOT EXISTS`` skips an existing table
        without locking it, indexes, columns, row-level security and column
        types are checked in the catalog first, and ``lock_timeout`` turns a
        wait that does happen into an error instead of a silent hang.
        """

        with self.backend.transaction():
            begin_migration(self.backend)
            apply_schema(self.backend, portable_statements())
            add_missing_columns(self.backend, ADDED_COLUMNS)
            if self.backend.dialect == "postgresql":
                for statement in tenant_isolation_statements(TENANT_TABLES, state=row_level_security_state(self.backend), policies=existing_policies(self.backend)):
                    self.backend.execute(statement)
                self._upgrade_postgres_numeric_columns()
            self.backend.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT (version) DO NOTHING",
                (SCHEMA_VERSION, now_iso()),
            )

    def _upgrade_postgres_numeric_columns(self) -> None:
        """Widen NUMBER/PERCENT columns created as REAL (float4) to DOUBLE PRECISION.

        Tables created by an earlier schema store money and percentages as
        4-byte floats, so ``SUM`` drifts by rupees. ``CREATE TABLE IF NOT
        EXISTS`` cannot fix an existing table, so this alters the columns that
        ``information_schema`` still reports as ``real``; afterwards it finds
        none and is a no-op.
        """

        wanted = postgres_numeric_columns()
        rows = self.backend.fetchall(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() AND data_type = 'real'"
        )
        stale = [(row["table_name"], row["column_name"]) for row in rows if (row["table_name"], row["column_name"]) in wanted]
        for table, column in stale:
            self.backend.execute(f"ALTER TABLE {_column(table)} ALTER COLUMN {_column(column)} TYPE DOUBLE PRECISION")

    def ping(self) -> bool:
        return self.backend.ping()

    def close(self) -> None:
        self.backend.close()

    def _tenant(self, institution_id: str):
        if not institution_id or not institution_id.strip():
            raise ValueError("institution_id is required")
        return self.backend.transaction(tenant_id=institution_id)

    # --------------------------------------------------------------- institutions
    def upsert_institution(self, institution_id: str, name: str, *, location: str = "", timezone_name: str = "Asia/Kolkata", settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        stamp = now_iso()
        with self.backend.transaction():
            self.backend.execute(
                """
                INSERT INTO institutions(institution_id, name, location, timezone, status, settings_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT (institution_id) DO UPDATE SET
                    name = EXCLUDED.name, location = EXCLUDED.location, timezone = EXCLUDED.timezone,
                    settings_json = EXCLUDED.settings_json, updated_at = EXCLUDED.updated_at
                """,
                (institution_id, name, location, timezone_name, _json(dict(settings or {})), stamp, stamp),
            )
        return self.get_institution(institution_id) or {}

    def get_institution(self, institution_id: str) -> dict[str, Any] | None:
        row = self.backend.fetchone("SELECT * FROM institutions WHERE institution_id = ?", (institution_id,))
        if row is None:
            return None
        row["settings"] = _loads(row.pop("settings_json", "{}"), {})
        return row

    def list_institutions(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.backend.fetchall("SELECT * FROM institutions ORDER BY name LIMIT ?", (_clamp_limit(limit),))
        for row in rows:
            row["settings"] = _loads(row.pop("settings_json", "{}"), {})
        return rows

    # ------------------------------------------------------------ entity records
    @staticmethod
    def _entity(entity_name: str) -> CanonicalEntity:
        return canonical_entity(entity_name)

    @staticmethod
    def _row_to_record(entity: CanonicalEntity, row: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {"record_key": row.get("record_key")}
        for item in entity.fields:
            value = row.get(item.name)
            if item.field_type is FieldType.BOOLEAN and value is not None:
                value = bool(value)
            record[item.name] = value
        record["attributes"] = _loads(row.get("attributes_json"), {})
        record["issues"] = _loads(row.get("issues_json"), [])
        record["lineage"] = {
            "source_file_id": row.get("source_file_id"),
            "source_file_name": row.get("source_file_name"),
            "source_locator": row.get("source_locator"),
            "ingestion_job_id": row.get("ingestion_job_id"),
            "imported_at": row.get("imported_at"),
            "updated_at": row.get("updated_at"),
        }
        return record

    def upsert_records(self, institution_id: str, records: Sequence[CanonicalRecord], *, skip_blocking: bool = True) -> ImportSummary:
        if not records:
            raise ValueError("no records to import")
        entity = self._entity(records[0].entity)
        if any(item.entity != entity.name for item in records):
            raise ValueError("all records in one import must share an entity")
        summary = ImportSummary(entity=entity.name)
        keys = [item.record_key for item in records]
        existing: dict[str, str] = {}
        for chunk in _chunks(keys, WRITE_CHUNK_ROWS):
            placeholders = ",".join("?" for _ in chunk)
            with self._tenant(institution_id):
                rows = self.backend.fetchall(
                    f"SELECT record_key, content_hash FROM {entity.table} WHERE institution_id = ? AND record_key IN ({placeholders})",
                    (institution_id, *chunk),
                )
            existing.update({row["record_key"]: row["content_hash"] for row in rows})
        stamp = now_iso()
        columns = ["row_id", "institution_id", "record_key", *entity.field_names(), "attributes_json", "normalizations_json", "issues_json", "content_hash", "source_file_id", "source_file_name", "source_locator", "ingestion_job_id", "imported_at", "updated_at"]
        update_columns = [name for name in columns if name not in {"row_id", "institution_id", "record_key", "imported_at"}]
        sql = (
            f"INSERT INTO {entity.table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT (institution_id, record_key) DO UPDATE SET " + ", ".join(f"{name} = EXCLUDED.{name}" for name in update_columns)
        )
        seen: set[str] = set()
        pending: list[tuple[str, str | None, list[Any]]] = []
        for record in records:
            key = record.record_key
            if not key.strip("|"):
                summary.skipped += 1
                summary.skipped_reasons["missing_natural_key"] = summary.skipped_reasons.get("missing_natural_key", 0) + 1
                continue
            if skip_blocking and record.has_blocking_issues():
                summary.skipped += 1
                summary.skipped_reasons["blocking_issues"] = summary.skipped_reasons.get("blocking_issues", 0) + 1
                continue
            if key in seen:
                summary.skipped += 1
                summary.skipped_reasons["duplicate_in_batch"] = summary.skipped_reasons.get("duplicate_in_batch", 0) + 1
                continue
            seen.add(key)
            content_hash = record.content_hash
            previous = existing.get(key)
            if previous == content_hash:
                summary.unchanged += 1
                continue
            values: list[Any] = [f"{entity.name}-{uuid4().hex}", institution_id, key]
            for item in entity.fields:
                value = record.fields.get(item.name)
                if item.field_type is FieldType.BOOLEAN and value is not None:
                    value = 1 if value else 0
                values.append(value)
            values.extend([
                _json(record.attributes), _json(list(record.normalizations)), _json(list(record.issues)), content_hash,
                record.lineage.source_file_id, record.lineage.source_file_name, record.lineage.source_locator, record.lineage.ingestion_job_id,
                stamp, stamp,
            ])
            pending.append((key, previous, values))
        # One transaction for the whole write, sent in bounded batches: the
        # import lands whole or not at all, so a failure can never leave rows
        # attributed to a job whose report says nothing was imported. Request
        # handlers call the store off the event loop, so holding the backend
        # lock for the import delays other store calls, never the API loop.
        with self._tenant(institution_id):
            for chunk in _chunks(pending, WRITE_CHUNK_ROWS):
                self.backend.executemany(sql, [values for _, _, values in chunk])
        for key, previous, _ in pending:
            if previous is None:
                summary.inserted += 1
                if len(summary.inserted_keys) < 50:
                    summary.inserted_keys.append(key)
            else:
                summary.updated += 1
                if len(summary.updated_keys) < 50:
                    summary.updated_keys.append(key)
        return summary

    def existing_keys(self, institution_id: str, entity_name: str, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        entity = self._entity(entity_name)
        found: dict[str, dict[str, Any]] = {}
        if not keys:
            return found
        with self._tenant(institution_id):
            for offset in range(0, len(keys), 500):
                chunk = list(keys[offset:offset + 500])
                placeholders = ",".join("?" for _ in chunk)
                rows = self.backend.fetchall(
                    f"SELECT * FROM {entity.table} WHERE institution_id = ? AND record_key IN ({placeholders})",
                    (institution_id, *chunk),
                )
                for row in rows:
                    found[row["record_key"]] = self._row_to_record(entity, row)
        return found

    def lookup_person_matches(self, institution_id: str, entity_name: str, *, names: Sequence[str] = (), phones: Sequence[str] = (), emails: Sequence[str] = (), limit: int = 2000) -> dict[str, dict[str, Any]]:
        """Existing people whose name, phone, or email matches a batch value (for duplicate review)."""

        entity = self._entity(entity_name)
        columns = set(entity.field_names())
        clauses: list[str] = []
        params: list[Any] = []
        for column, values in (("name", names), ("phone", phones), ("email", emails)):
            if column not in columns:
                continue
            cleaned = sorted({str(value).strip().lower() for value in values if value})[:500]
            if not cleaned:
                continue
            clauses.append(f"LOWER({column}) IN ({','.join('?' for _ in cleaned)})")
            params.extend(cleaned)
        if not clauses:
            return {}
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM {entity.table} WHERE institution_id = ? AND ({' OR '.join(clauses)}) LIMIT ?",
                (institution_id, *params, _clamp_limit(limit)),
            )
        return {row["record_key"]: self._row_to_record(entity, row) for row in rows}

    def _where(self, entity: CanonicalEntity, filters: Mapping[str, Any] | None) -> tuple[str, list[Any]]:
        clauses = ["institution_id = ?"]
        params: list[Any] = []
        for key, value in (filters or {}).items():
            if value is None or value == "":
                continue
            if key.endswith("__contains"):
                column = _column(key.removesuffix("__contains"))
                entity.field(column)
                clauses.append(f"LOWER({column}) LIKE ?")
                params.append(f"%{str(value).lower()}%")
                continue
            if key.endswith("__in"):
                column = _column(key.removesuffix("__in"))
                item = entity.field(column)
                values = list(value) if isinstance(value, (list, tuple, set)) else [value]
                if not values:
                    clauses.append("1 = 0")
                    continue
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(_bool_param(item, element) for element in values)
                continue
            if key.endswith("__gte") or key.endswith("__lte") or key.endswith("__lt") or key.endswith("__gt"):
                column, _, op = key.rpartition("__")
                _column(column)
                item = entity.field(column)
                clauses.append(f"{column} {({'gte': '>=', 'lte': '<=', 'lt': '<', 'gt': '>'})[op]} ?")
                params.append(_bool_param(item, value))
                continue
            column = _column(key)
            item = entity.field(column)
            value = _bool_param(item, value)
            if isinstance(value, str):
                clauses.append(f"LOWER({column}) = ?")
                params.append(value.lower())
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        return " AND ".join(clauses), params

    def query_records(self, institution_id: str, entity_name: str, filters: Mapping[str, Any] | None = None, *, limit: int = 100, offset: int = 0, order_by: str | None = None) -> list[dict[str, Any]]:
        entity = self._entity(entity_name)
        where, params = self._where(entity, filters)
        order = "record_key"
        if order_by:
            direction = "DESC" if order_by.startswith("-") else "ASC"
            column = _column(order_by.lstrip("-"))
            entity.field(column)
            order = f"{column} {direction}, record_key"
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM {entity.table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (institution_id, *params, _clamp_limit(limit), max(0, int(offset))),
            )
        return [self._row_to_record(entity, row) for row in rows]

    def count_records(self, institution_id: str, entity_name: str, filters: Mapping[str, Any] | None = None) -> int:
        entity = self._entity(entity_name)
        where, params = self._where(entity, filters)
        with self._tenant(institution_id):
            row = self.backend.fetchone(f"SELECT COUNT(*) AS total FROM {entity.table} WHERE {where}", (institution_id, *params))
        return int(row["total"]) if row else 0

    def get_record(self, institution_id: str, entity_name: str, record_key: str) -> dict[str, Any] | None:
        entity = self._entity(entity_name)
        with self._tenant(institution_id):
            row = self.backend.fetchone(
                f"SELECT * FROM {entity.table} WHERE institution_id = ? AND record_key = ?", (institution_id, record_key.lower()),
            )
        return self._row_to_record(entity, row) if row else None

    def update_record_fields(self, institution_id: str, entity_name: str, record_key: str, changes: Mapping[str, Any], *, locator: str) -> tuple[dict[str, Any], dict[str, Any]]:
        entity = self._entity(entity_name)
        if not changes:
            raise ValueError("no changes supplied")
        before = self.get_record(institution_id, entity_name, record_key)
        if before is None:
            raise KeyError(f"{entity_name} record not found: {record_key}")
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            column = _column(key)
            if column in entity.natural_key:
                raise ValueError(f"natural key field cannot be updated: {column}")
            item = entity.field(column)
            if item.field_type is FieldType.BOOLEAN and value is not None:
                value = 1 if value else 0
            assignments.append(f"{column} = ?")
            params.append(value)
        stamp = now_iso()
        assignments.append("updated_at = ?")
        params.append(stamp)
        assignments.append("source_locator = ?")
        params.append(locator)
        with self._tenant(institution_id):
            self.backend.execute(
                f"UPDATE {entity.table} SET {', '.join(assignments)} WHERE institution_id = ? AND record_key = ?",
                (*params, institution_id, record_key.lower()),
            )
            row = self.backend.fetchone(f"SELECT * FROM {entity.table} WHERE institution_id = ? AND record_key = ?", (institution_id, record_key.lower()))
        after = self._row_to_record(entity, row) if row else before
        # Keep the content hash honest so a later import can detect the change.
        return before, after

    def distinct_values(self, institution_id: str, entity_name: str, column: str, limit: int = 200) -> list[str]:
        entity = self._entity(entity_name)
        column = _column(column)
        entity.field(column)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT DISTINCT {column} AS value FROM {entity.table} WHERE institution_id = ? AND {column} IS NOT NULL AND {column} <> '' ORDER BY {column} LIMIT ?",
                (institution_id, _clamp_limit(limit)),
            )
        return [str(row["value"]) for row in rows]

    def entity_counts(self, institution_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._tenant(institution_id):
            for entity in CANONICAL_ENTITIES.values():
                row = self.backend.fetchone(f"SELECT COUNT(*) AS total FROM {entity.table} WHERE institution_id = ?", (institution_id,))
                counts[entity.name] = int(row["total"]) if row else 0
        return counts

    def delete_by_job(self, institution_id: str, entity_name: str, ingestion_job_id: str) -> int:
        entity = self._entity(entity_name)
        with self._tenant(institution_id):
            return self.backend.execute(
                f"DELETE FROM {entity.table} WHERE institution_id = ? AND ingestion_job_id = ?", (institution_id, ingestion_job_id),
            )

    # ------------------------------------------------------- specialised rollups
    def attendance_rollup(self, institution_id: str, *, program: str | None = None, semester: int | None = None, department: str | None = None, period: str | None = None, course_code: str | None = None) -> list[dict[str, Any]]:
        """Per-student attendance across the matching attendance rows, joined to names."""

        clauses = ["a.institution_id = ?"]
        params: list[Any] = [institution_id]
        if program:
            clauses.append("(LOWER(a.program) = ? OR (a.program IS NULL AND LOWER(s.program) = ?))")
            params.extend([program.lower(), program.lower()])
        if semester is not None:
            clauses.append("(a.semester = ? OR (a.semester IS NULL AND s.semester = ?))")
            params.extend([semester, semester])
        if department:
            clauses.append("(LOWER(a.department) = ? OR (a.department IS NULL AND LOWER(s.department) = ?))")
            params.extend([department.lower(), department.lower()])
        if period:
            clauses.append("LOWER(a.period) = ?")
            params.append(period.lower())
        if course_code:
            clauses.append("LOWER(a.course_code) = ?")
            params.append(course_code.lower())
        sql = f"""
            SELECT a.student_id AS student_id,
                   COALESCE(s.name, MAX(a.student_name)) AS name,
                   COALESCE(MAX(a.program), s.program) AS program,
                   COALESCE(MAX(a.semester), s.semester) AS semester,
                   COALESCE(MAX(a.department), s.department) AS department,
                   SUM(COALESCE(a.classes_held, 0)) AS classes_held,
                   SUM(COALESCE(a.classes_attended, 0)) AS classes_attended,
                   AVG(a.attendance_percent) AS avg_percent,
                   COUNT(*) AS rows_used
            FROM attendance a
            LEFT JOIN students s ON s.institution_id = a.institution_id AND LOWER(s.student_id) = LOWER(a.student_id)
            WHERE {' AND '.join(clauses)}
            GROUP BY a.student_id, s.name, s.program, s.semester, s.department
            ORDER BY a.student_id
            LIMIT ?
        """
        with self._tenant(institution_id):
            rows = self.backend.fetchall(sql, (*params, MAX_QUERY_ROWS * 4))
        rollup: list[dict[str, Any]] = []
        for row in rows:
            held = float(row.get("classes_held") or 0)
            attended = float(row.get("classes_attended") or 0)
            if held > 0:
                percent = round(attended / held * 100.0, 2)
            elif row.get("avg_percent") is not None:
                percent = round(float(row["avg_percent"]), 2)
            else:
                percent = None
            rollup.append({
                "student_id": row["student_id"],
                "name": row.get("name"),
                "program": row.get("program"),
                "semester": row.get("semester"),
                "department": row.get("department"),
                "classes_held": int(held),
                "classes_attended": int(attended),
                "attendance_percent": percent,
                "rows_used": int(row.get("rows_used") or 0),
            })
        return rollup

    def fee_rollup(self, institution_id: str, *, program: str | None = None, semester: int | None = None, academic_year: str | None = None, fee_type: str | None = None) -> list[dict[str, Any]]:
        clauses = ["f.institution_id = ?"]
        params: list[Any] = [institution_id]
        if program:
            clauses.append("(LOWER(f.program) = ? OR (f.program IS NULL AND LOWER(s.program) = ?))")
            params.extend([program.lower(), program.lower()])
        if semester is not None:
            clauses.append("(f.semester = ? OR (f.semester IS NULL AND s.semester = ?))")
            params.extend([semester, semester])
        if academic_year:
            clauses.append("LOWER(f.academic_year) = ?")
            params.append(academic_year.lower())
        if fee_type:
            clauses.append("LOWER(f.fee_type) = ?")
            params.append(fee_type.lower())
        sql = f"""
            SELECT f.student_id AS student_id, COALESCE(s.name, MAX(f.student_name)) AS name,
                   COALESCE(MAX(f.program), s.program) AS program, COALESCE(MAX(f.semester), s.semester) AS semester,
                   SUM(COALESCE(f.amount_due, 0)) AS amount_due, SUM(COALESCE(f.amount_paid, 0)) AS amount_paid,
                   SUM(COALESCE(f.balance, COALESCE(f.amount_due, 0) - COALESCE(f.amount_paid, 0))) AS balance,
                   MIN(f.due_date) AS earliest_due_date, COUNT(*) AS rows_used
            FROM fees f
            LEFT JOIN students s ON s.institution_id = f.institution_id AND LOWER(s.student_id) = LOWER(f.student_id)
            WHERE {' AND '.join(clauses)}
            GROUP BY f.student_id, s.name, s.program, s.semester
            ORDER BY balance DESC, f.student_id
            LIMIT ?
        """
        with self._tenant(institution_id):
            rows = self.backend.fetchall(sql, (*params, MAX_QUERY_ROWS * 4))
        return [
            {
                "student_id": row["student_id"], "name": row.get("name"), "program": row.get("program"), "semester": row.get("semester"),
                "amount_due": round(float(row.get("amount_due") or 0), 2), "amount_paid": round(float(row.get("amount_paid") or 0), 2),
                "balance": round(float(row.get("balance") or 0), 2), "earliest_due_date": row.get("earliest_due_date"), "rows_used": int(row.get("rows_used") or 0),
            }
            for row in rows
        ]

    def exam_rollup(self, institution_id: str, *, program: str | None = None, semester: int | None = None, course_code: str | None = None, exam_name: str | None = None) -> list[dict[str, Any]]:
        clauses = ["e.institution_id = ?"]
        params: list[Any] = [institution_id]
        if program:
            clauses.append("(LOWER(e.program) = ? OR (e.program IS NULL AND LOWER(s.program) = ?))")
            params.extend([program.lower(), program.lower()])
        if semester is not None:
            clauses.append("(e.semester = ? OR (e.semester IS NULL AND s.semester = ?))")
            params.extend([semester, semester])
        for column, value in (("course_code", course_code), ("exam_name", exam_name)):
            if value:
                clauses.append(f"LOWER(e.{column}) = ?")
                params.append(value.lower())
        sql = f"""
            SELECT e.course_code AS course_code, MAX(e.course_name) AS course_name, e.exam_name AS exam_name, COUNT(*) AS students,
                   AVG(CASE WHEN e.max_marks IS NOT NULL AND e.max_marks > 0 AND e.marks_obtained IS NOT NULL THEN e.marks_obtained * 100.0 / e.max_marks END) AS avg_percent,
                   SUM(CASE WHEN LOWER(COALESCE(e.result_status, '')) IN ('pass', 'passed', 'p') THEN 1 ELSE 0 END) AS passed,
                   SUM(CASE WHEN LOWER(COALESCE(e.result_status, '')) IN ('fail', 'failed', 'f', 'reappear', 'absent') THEN 1 ELSE 0 END) AS failed
            FROM exams e
            LEFT JOIN students s ON s.institution_id = e.institution_id AND LOWER(s.student_id) = LOWER(e.student_id)
            WHERE {' AND '.join(clauses)}
            GROUP BY e.course_code, e.exam_name ORDER BY e.course_code, e.exam_name LIMIT ?
        """
        with self._tenant(institution_id):
            rows = self.backend.fetchall(sql, (*params, MAX_QUERY_ROWS))
        return [
            {
                "course_code": row.get("course_code"), "course_name": row.get("course_name"), "exam_name": row.get("exam_name"),
                "students": int(row.get("students") or 0), "avg_percent": round(float(row["avg_percent"]), 2) if row.get("avg_percent") is not None else None,
                "passed": int(row.get("passed") or 0), "failed": int(row.get("failed") or 0),
            }
            for row in rows
        ]

    # ----------------------------------------------------------- ingestion state
    def record_file(self, institution_id: str, *, file_id: str, file_name: str, content_type: str, size_bytes: int, sha256: str, object_key: str, file_kind: str, uploaded_by: str) -> dict[str, Any]:
        stamp = now_iso()
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO ingestion_files(file_id, institution_id, file_name, content_type, size_bytes, sha256, object_key, file_kind, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, institution_id, file_name, content_type, size_bytes, sha256, object_key, file_kind, uploaded_by, stamp),
            )
        return self.get_file(institution_id, file_id) or {}

    def get_file(self, institution_id: str, file_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT * FROM ingestion_files WHERE institution_id = ? AND file_id = ?", (institution_id, file_id))

    def create_job(self, institution_id: str, *, job_id: str, file_id: str | None, entity: str | None, requested_by: str, source_kind: str = "upload", options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        stamp = now_iso()
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO ingestion_jobs(job_id, institution_id, file_id, entity, status, stage, source_kind, requested_by, created_at, updated_at, options_json) VALUES (?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?, ?)",
                (job_id, institution_id, file_id, entity, source_kind, requested_by, stamp, stamp, _json(dict(options or {}))),
            )
        return self.get_job(institution_id, job_id) or {}

    def update_job(self, institution_id: str, job_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "entity", "mapping", "report", "error", "row_count", "sheet_name", "options"}
        assignments: list[str] = ["updated_at = ?"]
        params: list[Any] = [now_iso()]
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"job field cannot be updated: {key}")
            if key in {"mapping", "report", "options"}:
                assignments.append(f"{key}_json = ?")
                params.append(_json(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        with self._tenant(institution_id):
            self.backend.execute(
                f"UPDATE ingestion_jobs SET {', '.join(assignments)} WHERE institution_id = ? AND job_id = ?", (*params, institution_id, job_id),
            )
        return self.get_job(institution_id, job_id) or {}

    @staticmethod
    def _job_row(row: dict[str, Any]) -> dict[str, Any]:
        row["mapping"] = _loads(row.pop("mapping_json", "{}"), {})
        row["report"] = _loads(row.pop("report_json", "{}"), {})
        row["options"] = _loads(row.pop("options_json", "{}"), {})
        return row

    def get_job(self, institution_id: str, job_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM ingestion_jobs WHERE institution_id = ? AND job_id = ?", (institution_id, job_id))
        return self._job_row(row) if row else None

    def list_jobs(self, institution_id: str, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [institution_id]
        clause = ""
        if status:
            clause = " AND status = ?"
            params.append(status)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM ingestion_jobs WHERE institution_id = ?{clause} ORDER BY created_at DESC LIMIT ?", (*params, _clamp_limit(limit, 500)),
            )
        return [self._job_row(row) for row in rows]

    def replace_job_records(self, institution_id: str, job_id: str, records: Iterable[Mapping[str, Any]]) -> int:
        """Replace a job's staged rows atomically, inserting in bounded batches.

        The delete and the inserts share one transaction, so a reader never
        sees a half-staged job and a failure leaves the previous rows intact.
        """

        count = 0
        rows = (
            (
                f"rec-{uuid4().hex}", job_id, institution_id, int(item["row_number"]), str(item["locator"]),
                _json(item.get("raw", {})), _json(item.get("normalized", {})), str(item.get("status", "pending")),
                str(item.get("action", "pending")), item.get("record_key"), _json(list(item.get("issues", []))),
            )
            for item in records
        )
        with self._tenant(institution_id):
            self.backend.execute("DELETE FROM ingestion_records WHERE institution_id = ? AND job_id = ?", (institution_id, job_id))
            for chunk in _chunks(rows, WRITE_CHUNK_ROWS):
                count += self.backend.executemany(
                    "INSERT INTO ingestion_records(record_id, job_id, institution_id, row_number, locator, raw_json, normalized_json, status, action, record_key, issues_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    chunk,
                )
        return count

    def job_records(self, institution_id: str, job_id: str, *, limit: int = 1000, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [institution_id, job_id]
        clause = ""
        if status:
            clause = " AND status = ?"
            params.append(status)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM ingestion_records WHERE institution_id = ? AND job_id = ?{clause} ORDER BY row_number LIMIT ? OFFSET ?",
                (*params, _clamp_limit(limit, 50_000), max(0, offset)),
            )
        for row in rows:
            row["raw"] = _loads(row.pop("raw_json", "{}"), {})
            row["normalized"] = _loads(row.pop("normalized_json", "{}"), {})
            row["issues"] = _loads(row.pop("issues_json", "[]"), [])
        return rows

    def add_review_items(self, institution_id: str, job_id: str, items: Iterable[Mapping[str, Any]]) -> list[str]:
        ids: list[str] = []
        stamp = now_iso()
        rows = []
        for position, item in enumerate(items):
            # Items added together share created_at; the position keeps them in the order given (file order).
            review_id = f"review-{position:06d}{uuid4().hex}"
            ids.append(review_id)
            rows.append((review_id, institution_id, job_id, str(item["kind"]), "pending", _json(dict(item.get("payload", {}))), stamp))
        if rows:
            with self._tenant(institution_id):
                self.backend.executemany(
                    "INSERT INTO review_items(review_id, institution_id, job_id, kind, status, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
                )
        return ids

    def list_review_items(self, institution_id: str, *, job_id: str | None = None, status: str | None = "pending", limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        clauses = ["institution_id = ?"]
        params: list[Any] = [institution_id]
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        # Items added together share created_at; the id keeps pages stable.
        with self._tenant(institution_id):
            rows = self.backend.fetchall(
                f"SELECT * FROM review_items WHERE {' AND '.join(clauses)} ORDER BY created_at, review_id LIMIT ? OFFSET ?", (*params, _clamp_limit(limit, 2000), max(0, offset)),
            )
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json", "{}"), {})
            row["resolution"] = _loads(row.pop("resolution_json", "{}"), {})
        return rows

    def get_review_item(self, institution_id: str, review_id: str) -> dict[str, Any] | None:
        """One review item of the tenant, decoded like ``list_review_items``; ``None`` when absent."""

        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM review_items WHERE institution_id = ? AND review_id = ?", (institution_id, review_id))
        if row is None:
            return None
        row["payload"] = _loads(row.pop("payload_json", "{}"), {})
        row["resolution"] = _loads(row.pop("resolution_json", "{}"), {})
        return row

    def resolve_review_item(self, institution_id: str, review_id: str, *, status: str, resolved_by: str, resolution: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        if status not in {"approved", "rejected"}:
            raise ValueError("review status must be approved or rejected")
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE review_items SET status = ?, resolved_at = ?, resolved_by = ?, resolution_json = ? WHERE institution_id = ? AND review_id = ? AND status = 'pending'",
                (status, now_iso(), resolved_by, _json(dict(resolution or {})), institution_id, review_id),
            )
            row = self.backend.fetchone("SELECT * FROM review_items WHERE institution_id = ? AND review_id = ?", (institution_id, review_id))
        if row is None:
            return None
        row["payload"] = _loads(row.pop("payload_json", "{}"), {})
        row["resolution"] = _loads(row.pop("resolution_json", "{}"), {})
        return row

    def save_mapping_profile(self, institution_id: str, *, entity: str, header_signature: str, mapping: Mapping[str, Any], approved_by: str) -> str:
        profile_id = f"map-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute(
                """
                INSERT INTO mapping_profiles(profile_id, institution_id, entity, header_signature, mapping_json, approved_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (institution_id, entity, header_signature) DO UPDATE SET mapping_json = EXCLUDED.mapping_json, approved_by = EXCLUDED.approved_by, created_at = EXCLUDED.created_at
                """,
                (profile_id, institution_id, entity, header_signature, _json(dict(mapping)), approved_by, now_iso()),
            )
        return profile_id

    def find_mapping_profile(self, institution_id: str, entity: str, header_signature: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone(
                "SELECT * FROM mapping_profiles WHERE institution_id = ? AND entity = ? AND header_signature = ?", (institution_id, entity, header_signature),
            )
        if row is None:
            return None
        row["mapping"] = _loads(row.pop("mapping_json", "{}"), {})
        return row

    def find_latest_mapping_profile(self, institution_id: str, header_signature: str) -> dict[str, Any] | None:
        """The most recently approved profile for these headers, whichever entity the reviewer chose."""

        with self._tenant(institution_id):
            row = self.backend.fetchone(
                "SELECT * FROM mapping_profiles WHERE institution_id = ? AND header_signature = ? ORDER BY created_at DESC, profile_id DESC LIMIT 1", (institution_id, header_signature),
            )
        if row is None:
            return None
        row["mapping"] = _loads(row.pop("mapping_json", "{}"), {})
        return row

    # ---------------------------------------------------------------- documents
    def add_document(self, institution_id: str, *, document_id: str, title: str, file_name: str, object_key: str, content_type: str, size_bytes: int, sha256: str, classification: str, category: str, uploaded_by: str, page_count: int, chunks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        stamp = now_iso()
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO documents(document_id, institution_id, title, file_name, object_key, content_type, size_bytes, sha256, classification, category, uploaded_by, uploaded_at, page_count, chunk_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed')",
                (document_id, institution_id, title, file_name, object_key, content_type, size_bytes, sha256, classification, category, uploaded_by, stamp, page_count, len(chunks)),
            )
            self.backend.executemany(
                "INSERT INTO document_chunks(chunk_id, institution_id, document_id, chunk_index, page_number, text, embedding_json, token_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (f"chunk-{uuid4().hex}", institution_id, document_id, int(item["chunk_index"]), item.get("page_number"), str(item["text"]), _json(list(item["embedding"])), int(item.get("token_count", 0)))
                    for item in chunks
                ),
            )
        return self.get_document(institution_id, document_id) or {}

    def get_document(self, institution_id: str, document_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            return self.backend.fetchone("SELECT * FROM documents WHERE institution_id = ? AND document_id = ?", (institution_id, document_id))

    def list_documents(self, institution_id: str, *, limit: int = 100, category: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [institution_id]
        clause = ""
        if category:
            clause = " AND category = ?"
            params.append(category)
        with self._tenant(institution_id):
            return self.backend.fetchall(
                f"SELECT * FROM documents WHERE institution_id = ?{clause} ORDER BY uploaded_at DESC LIMIT ?", (*params, _clamp_limit(limit, 1000)),
            )

    def delete_document(self, institution_id: str, document_id: str) -> bool:
        with self._tenant(institution_id):
            self.backend.execute("DELETE FROM document_chunks WHERE institution_id = ? AND document_id = ?", (institution_id, document_id))
            return self.backend.execute("DELETE FROM documents WHERE institution_id = ? AND document_id = ?", (institution_id, document_id)) > 0

    def document_chunks(self, institution_id: str, *, classifications: Sequence[str] | None = None, document_ids: Sequence[str] | None = None, limit: int = 20_000) -> list[dict[str, Any]]:
        clauses = ["c.institution_id = ?"]
        params: list[Any] = [institution_id]
        if classifications:
            clauses.append(f"d.classification IN ({','.join('?' for _ in classifications)})")
            params.extend(classifications)
        if document_ids:
            clauses.append(f"c.document_id IN ({','.join('?' for _ in document_ids)})")
            params.extend(document_ids)
        sql = f"""
            SELECT c.chunk_id, c.document_id, c.chunk_index, c.page_number, c.text, c.embedding_json, d.title, d.classification, d.category, d.file_name
            FROM document_chunks c JOIN documents d ON d.document_id = c.document_id AND d.institution_id = c.institution_id
            WHERE {' AND '.join(clauses)} ORDER BY c.document_id, c.chunk_index LIMIT ?
        """
        with self._tenant(institution_id):
            rows = self.backend.fetchall(sql, (*params, _clamp_limit(limit, 100_000)))
        for row in rows:
            row["embedding"] = _loads(row.pop("embedding_json", "[]"), [])
        return rows

    # ------------------------------------------------------------------ reports
    def add_report(self, institution_id: str, *, report_id: str, title: str, format_name: str, object_key: str, size_bytes: int, sha256: str, row_count: int, tool_name: str, created_by: str, required_capabilities: Sequence[str] = ()) -> dict[str, Any]:
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO generated_reports(report_id, institution_id, title, format, object_key, size_bytes, sha256, row_count, tool_name, created_by, created_at, required_capabilities_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, institution_id, title, format_name, object_key, size_bytes, sha256, row_count, tool_name, created_by, now_iso(), _json(sorted({str(item) for item in required_capabilities}))),
            )
        return self.get_report(institution_id, report_id) or {}

    @staticmethod
    def _decode_report(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.pop("required_capabilities_json", None)
        decoded = _loads(raw, None) if raw not in (None, "") else None
        # None means the requirements were never recorded (a row that predates
        # the column); the report service then shows the report to its creator only.
        row["required_capabilities"] = [str(item) for item in decoded] if isinstance(decoded, list) else None
        return row

    def get_report(self, institution_id: str, report_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM generated_reports WHERE institution_id = ? AND report_id = ?", (institution_id, report_id))
        return self._decode_report(row) if row is not None else None

    def list_reports(self, institution_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT * FROM generated_reports WHERE institution_id = ? ORDER BY created_at DESC LIMIT ?", (institution_id, _clamp_limit(limit, 500)))
        return [self._decode_report(row) for row in rows]

    # ------------------------------------------------------------ notifications
    def add_notification(self, institution_id: str, *, recipient_id: str, title: str, body: str, created_by: str, channel: str = "in_app", reference_type: str | None = None, reference_id: str | None = None) -> str:
        notification_id = f"ntf-{uuid4().hex}"
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO notifications(notification_id, institution_id, recipient_id, title, body, channel, status, reference_type, reference_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?, ?)",
                (notification_id, institution_id, recipient_id, title, body, channel, reference_type, reference_id, created_by, now_iso()),
            )
        return notification_id

    def list_notifications(self, institution_id: str, recipient_id: str, *, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        clause = " AND status = 'unread'" if unread_only else ""
        with self._tenant(institution_id):
            return self.backend.fetchall(
                f"SELECT * FROM notifications WHERE institution_id = ? AND recipient_id = ?{clause} ORDER BY created_at DESC LIMIT ?",
                (institution_id, recipient_id, _clamp_limit(limit, 500)),
            )

    def mark_notification_read(self, institution_id: str, recipient_id: str, notification_id: str) -> bool:
        with self._tenant(institution_id):
            return self.backend.execute(
                "UPDATE notifications SET status = 'read', read_at = ? WHERE institution_id = ? AND recipient_id = ? AND notification_id = ?",
                (now_iso(), institution_id, recipient_id, notification_id),
            ) > 0

    # ------------------------------------------------------------------- email
    def add_email(self, institution_id: str, *, email_id: str, recipients: Sequence[str], subject: str, body: str, attachments: Sequence[Mapping[str, Any]], status: str, provider: str, provider_message_id: str | None, created_by: str, error: str | None = None) -> dict[str, Any]:
        stamp = now_iso()
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO email_outbox(email_id, institution_id, recipients_json, subject, body, attachments_json, status, provider, provider_message_id, created_by, created_at, sent_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email_id, institution_id, _json(list(recipients)), subject, body, _json([dict(item) for item in attachments]), status, provider, provider_message_id, created_by, stamp, stamp if status == "sent" else None, error),
            )
            row = self.backend.fetchone("SELECT * FROM email_outbox WHERE institution_id = ? AND email_id = ?", (institution_id, email_id))
        if row:
            row["recipients"] = _loads(row.pop("recipients_json", "[]"), [])
            row["attachments"] = _loads(row.pop("attachments_json", "[]"), [])
        return row or {}

    def list_emails(self, institution_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._tenant(institution_id):
            rows = self.backend.fetchall("SELECT * FROM email_outbox WHERE institution_id = ? ORDER BY created_at DESC LIMIT ?", (institution_id, _clamp_limit(limit, 500)))
        for row in rows:
            row["recipients"] = _loads(row.pop("recipients_json", "[]"), [])
            row["attachments"] = _loads(row.pop("attachments_json", "[]"), [])
        return rows

    # ------------------------------------------------------------- agent runs
    def add_agent_run(self, institution_id: str, *, run_id: str, principal_id: str, request_id: str, channel: str, command_sha256: str, status: str, plan: Sequence[Mapping[str, Any]], steps: Sequence[Mapping[str, Any]], tool_names: Sequence[str], duration_ms: int) -> None:
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO agent_runs(run_id, institution_id, principal_id, request_id, channel, command_sha256, status, plan_json, steps_json, tool_names_json, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, institution_id, principal_id, request_id, channel, command_sha256, status, _json([dict(item) for item in plan]), _json([dict(item) for item in steps]), _json(list(tool_names)), duration_ms, now_iso()),
            )

    def list_agent_runs(self, institution_id: str, *, principal_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = [institution_id]
        clause = ""
        if principal_id:
            clause = " AND principal_id = ?"
            params.append(principal_id)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM agent_runs WHERE institution_id = ?{clause} ORDER BY created_at DESC LIMIT ?", (*params, _clamp_limit(limit, 500)))
        for row in rows:
            row["plan"] = _loads(row.pop("plan_json", "[]"), [])
            row["steps"] = _loads(row.pop("steps_json", "[]"), [])
            row["tool_names"] = _loads(row.pop("tool_names_json", "[]"), [])
        return rows

    # -------------------------------------------------------------- approvals
    def create_approval(self, institution_id: str, *, approval_id: str, principal_id: str, tool_name: str, arguments: Mapping[str, Any], arguments_sha256: str, reason: str, ttl_seconds: int) -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        with self._tenant(institution_id):
            self.backend.execute(
                "INSERT INTO approvals(approval_id, institution_id, principal_id, tool_name, arguments_json, arguments_sha256, status, reason, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (approval_id, institution_id, principal_id, tool_name, _json(dict(arguments)), arguments_sha256, reason, created.isoformat(), (created + timedelta(seconds=ttl_seconds)).isoformat()),
            )
        return self.get_approval(institution_id, approval_id) or {}

    def get_approval(self, institution_id: str, approval_id: str) -> dict[str, Any] | None:
        with self._tenant(institution_id):
            row = self.backend.fetchone("SELECT * FROM approvals WHERE institution_id = ? AND approval_id = ?", (institution_id, approval_id))
        if row:
            row["arguments"] = _loads(row.pop("arguments_json", "{}"), {})
            row["resume"] = _loads(row.pop("resume_json", None), None)
        return row

    def set_approval_resume(self, institution_id: str, approval_id: str, *, principal_id: str, resume: Mapping[str, Any]) -> bool:
        """Keep, with a pending confirmation, what its confirmed re-send resumes (the rest of the plan)."""

        with self._tenant(institution_id):
            changed = self.backend.execute(
                "UPDATE approvals SET resume_json = ? WHERE institution_id = ? AND approval_id = ? AND principal_id = ? AND status = 'pending'",
                (_json(dict(resume)), institution_id, approval_id, principal_id),
            )
        return changed == 1

    def claim_approval(self, institution_id: str, approval_id: str, *, principal_id: str) -> dict[str, Any] | None:
        """Atomically move one approved, unexpired approval to ``executing``.

        Exactly one caller wins when several carry the same approval id, so a
        high-risk action can never run twice on one confirmation. Returns the
        record when this call claimed it, otherwise ``None``.
        """

        with self._tenant(institution_id):
            claimed = self.backend.execute(
                "UPDATE approvals SET status = 'executing', decided_at = ?, decided_by = ? WHERE institution_id = ? AND approval_id = ? AND principal_id = ? AND status = 'approved' AND expires_at > ?",
                (now_iso(), principal_id, institution_id, approval_id, principal_id, now_iso()),
            )
        return self.get_approval(institution_id, approval_id) if claimed else None

    def decide_approval(self, institution_id: str, approval_id: str, *, status: str, decided_by: str) -> dict[str, Any] | None:
        if status not in {"approved", "rejected", "consumed", "expired"}:
            raise ValueError("approval status must be approved, rejected, consumed, or expired")
        with self._tenant(institution_id):
            self.backend.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE institution_id = ? AND approval_id = ?",
                (status, now_iso(), decided_by, institution_id, approval_id),
            )
        return self.get_approval(institution_id, approval_id)

    def list_approvals(self, institution_id: str, *, principal_id: str | None = None, status: str | None = "pending", limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["institution_id = ?"]
        params: list[Any] = [institution_id]
        if principal_id:
            clauses.append("principal_id = ?")
            params.append(principal_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._tenant(institution_id):
            rows = self.backend.fetchall(f"SELECT * FROM approvals WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?", (*params, _clamp_limit(limit, 500)))
        for row in rows:
            row["arguments"] = _loads(row.pop("arguments_json", "{}"), {})
            row.pop("resume_json", None)  # internal: what a confirmed re-send resumes
        return rows

    # -------------------------------------------------------- background jobs
    def enqueue_background_job(self, institution_id: str, *, job_id: str, job_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.backend.transaction():
            self.backend.execute(
                "INSERT INTO background_jobs(job_id, institution_id, job_type, payload_json, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                (job_id, institution_id, job_type, _json(dict(payload)), now_iso()),
            )
        return self.get_background_job(job_id) or {}

    def get_background_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.backend.fetchone("SELECT * FROM background_jobs WHERE job_id = ?", (job_id,))
        if row:
            row["payload"] = _loads(row.pop("payload_json", "{}"), {})
            row["result"] = _loads(row.pop("result_json", "{}"), {})
        return row

    def claim_background_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self.backend.transaction():
            rows = self.backend.fetchall("SELECT job_id FROM background_jobs WHERE status = 'queued' ORDER BY created_at LIMIT ?", (_clamp_limit(limit, 100),))
            for row in rows:
                stamp = now_iso()
                updated = self.backend.execute(
                    "UPDATE background_jobs SET status = 'running', started_at = ?, heartbeat_at = ?, attempts = attempts + 1 WHERE job_id = ? AND status = 'queued'",
                    (stamp, stamp, row["job_id"]),
                )
                if updated:
                    job = self.get_background_job(row["job_id"])
                    if job:
                        claimed.append(job)
        return claimed

    def claim_background_job(self, job_id: str) -> dict[str, Any] | None:
        """Claim exactly one queued job (used by message-driven queues such as SQS).

        Returns the job when this call moved it from ``queued`` to ``running``;
        ``None`` when it does not exist or was already claimed or finished.
        """

        stamp = now_iso()
        with self.backend.transaction():
            updated = self.backend.execute(
                "UPDATE background_jobs SET status = 'running', started_at = ?, heartbeat_at = ?, attempts = attempts + 1 WHERE job_id = ? AND status = 'queued'",
                (stamp, stamp, job_id),
            )
        return self.get_background_job(job_id) if updated else None

    def heartbeat_background_job(self, job_id: str) -> bool:
        """Record that the worker running this job is still alive."""

        with self.backend.transaction():
            return self.backend.execute("UPDATE background_jobs SET heartbeat_at = ? WHERE job_id = ? AND status = 'running'", (now_iso(), job_id)) > 0

    def requeue_background_job(self, job_id: str) -> bool:
        """Hand a running job back to the queue (a worker is shutting down mid-job)."""

        with self.backend.transaction():
            return self.backend.execute("UPDATE background_jobs SET status = 'queued', started_at = NULL, heartbeat_at = NULL WHERE job_id = ? AND status = 'running'", (job_id,)) > 0

    def requeue_stale_background_jobs(self, *, older_than_seconds: float, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Return ``running`` jobs whose worker stopped reporting to ``queued`` so another worker resumes them.

        A running worker refreshes ``heartbeat_at`` while it works; a job whose
        last heartbeat (or claim) is older than ``older_than_seconds`` has no
        live worker and goes back to ``queued`` unless it has exhausted
        ``max_attempts``, in which case it is marked ``failed`` so nothing
        loops forever. A job whose heartbeat is fresh is never touched.
        """

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0.001, float(older_than_seconds)))).isoformat()
        requeued: list[dict[str, Any]] = []
        with self.backend.transaction():
            rows = self.backend.fetchall(
                "SELECT job_id, attempts FROM background_jobs WHERE status = 'running' AND COALESCE(heartbeat_at, started_at, created_at) < ? ORDER BY created_at LIMIT 500",
                (cutoff,),
            )
            for row in rows:
                if int(row["attempts"] or 0) >= max_attempts:
                    self.backend.execute(
                        "UPDATE background_jobs SET status = 'failed', finished_at = ?, error = ? WHERE job_id = ? AND status = 'running'",
                        (now_iso(), "worker did not finish the job after repeated attempts", row["job_id"]),
                    )
                    continue
                updated = self.backend.execute(
                    "UPDATE background_jobs SET status = 'queued', started_at = NULL, heartbeat_at = NULL WHERE job_id = ? AND status = 'running'",
                    (row["job_id"],),
                )
                if updated:
                    job = self.get_background_job(row["job_id"])
                    if job:
                        requeued.append(job)
        return requeued

    def finish_background_job(self, job_id: str, *, status: str, result: Mapping[str, Any] | None = None, error: str | None = None, attempt: int | None = None) -> bool:
        """Record a job's outcome; with ``attempt`` only that attempt's outcome counts.

        A run that was handed back to the queue and claimed again has a higher
        attempt number, so the abandoned run finishing late cannot overwrite it.
        """

        if status not in {"succeeded", "failed"}:
            raise ValueError("background job status must be succeeded or failed")
        clause = "" if attempt is None else " AND attempts = ?"
        params: list[Any] = [status, now_iso(), _json(dict(result or {})), error, job_id]
        if attempt is not None:
            params.append(int(attempt))
        with self.backend.transaction():
            return self.backend.execute(
                f"UPDATE background_jobs SET status = ?, finished_at = ?, result_json = ?, error = ? WHERE job_id = ? AND status IN ('queued', 'running'){clause}",
                tuple(params),
            ) > 0

    def list_background_jobs(self, institution_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.backend.fetchall("SELECT * FROM background_jobs WHERE institution_id = ? ORDER BY created_at DESC LIMIT ?", (institution_id, _clamp_limit(limit, 500)))
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json", "{}"), {})
            row["result"] = _loads(row.pop("result_json", "{}"), {})
        return rows


__all__ = ["InstitutionDataStore", "MAX_QUERY_ROWS", "WRITE_CHUNK_ROWS", "now_iso"]
