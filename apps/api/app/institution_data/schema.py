"""DDL for the canonical institution database.

Entity tables are generated from the canonical model so a new field only has
to be declared once. Every tenant table carries ``institution_id`` and, on
PostgreSQL, a row-level-security policy bound to ``app.institution_id``.
"""

from __future__ import annotations

from ..normalization.canonical import CANONICAL_ENTITIES, CanonicalEntity, FieldType

SCHEMA_VERSION = "002_institution_data"

LINEAGE_COLUMNS = (
    "source_file_id",
    "source_file_name",
    "source_locator",
    "ingestion_job_id",
)

_TYPE_MAP = {
    FieldType.INTEGER: "INTEGER",
    FieldType.NUMBER: "DOUBLE PRECISION",
    FieldType.PERCENT: "DOUBLE PRECISION",
    FieldType.BOOLEAN: "INTEGER",
}


def column_type(field_type: FieldType) -> str:
    return _TYPE_MAP.get(field_type, "TEXT")


def entity_table_ddl(entity: CanonicalEntity) -> str:
    columns = [
        "row_id TEXT PRIMARY KEY",
        "institution_id TEXT NOT NULL",
        "record_key TEXT NOT NULL",
    ]
    for item in entity.fields:
        columns.append(f"{item.name} {column_type(item.field_type)}")
    columns.extend([
        "attributes_json TEXT NOT NULL DEFAULT '{}'",
        "normalizations_json TEXT NOT NULL DEFAULT '[]'",
        "issues_json TEXT NOT NULL DEFAULT '[]'",
        "content_hash TEXT NOT NULL",
        "source_file_id TEXT",
        "source_file_name TEXT",
        "source_locator TEXT",
        "ingestion_job_id TEXT",
        "imported_at TEXT NOT NULL",
        "updated_at TEXT NOT NULL",
        "UNIQUE(institution_id, record_key)",
    ])
    return f"CREATE TABLE IF NOT EXISTS {entity.table} (\n    " + ",\n    ".join(columns) + "\n)"


TENANT_TABLES: tuple[str, ...] = tuple(entity.table for entity in CANONICAL_ENTITIES.values()) + (
    "ingestion_files",
    "ingestion_jobs",
    "ingestion_records",
    "review_items",
    "mapping_profiles",
    "documents",
    "document_chunks",
    "generated_reports",
    "notifications",
    "email_outbox",
    "agent_runs",
    "approvals",
)


def portable_statements() -> tuple[str, ...]:
    statements: list[str] = [
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
        """
        CREATE TABLE IF NOT EXISTS institutions (
            institution_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
            status TEXT NOT NULL DEFAULT 'active',
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]
    for entity in CANONICAL_ENTITIES.values():
        statements.append(entity_table_ddl(entity))
        statements.append(f"CREATE INDEX IF NOT EXISTS idx_{entity.table}_institution ON {entity.table}(institution_id)")
    statements.extend([
        """
        CREATE TABLE IF NOT EXISTS ingestion_files (
            file_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            object_key TEXT NOT NULL,
            file_kind TEXT NOT NULL DEFAULT 'unknown',
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ingestion_files_institution ON ingestion_files(institution_id, uploaded_at)",
        """
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            job_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            file_id TEXT,
            entity TEXT,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'upload',
            requested_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            mapping_json TEXT NOT NULL DEFAULT '{}',
            report_json TEXT NOT NULL DEFAULT '{}',
            options_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            row_count INTEGER NOT NULL DEFAULT 0,
            sheet_name TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_institution ON ingestion_jobs(institution_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS ingestion_records (
            record_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            locator TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'pending',
            record_key TEXT,
            issues_json TEXT NOT NULL DEFAULT '[]'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ingestion_records_job ON ingestion_records(job_id, row_number)",
        """
        CREATE TABLE IF NOT EXISTS review_items (
            review_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT,
            resolution_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_review_items_job ON review_items(institution_id, job_id, status)",
        """
        CREATE TABLE IF NOT EXISTS mapping_profiles (
            profile_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            header_signature TEXT NOT NULL,
            mapping_json TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(institution_id, entity, header_signature)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            title TEXT NOT NULL,
            file_name TEXT NOT NULL,
            object_key TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal',
            category TEXT NOT NULL DEFAULT 'general',
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'indexed'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_documents_institution ON documents(institution_id, uploaded_at)",
        """
        CREATE TABLE IF NOT EXISTS document_chunks (
            chunk_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,
            text TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks(institution_id, document_id, chunk_index)",
        """
        CREATE TABLE IF NOT EXISTS generated_reports (
            report_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            title TEXT NOT NULL,
            format TEXT NOT NULL,
            object_key TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            tool_name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            required_capabilities_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_institution ON generated_reports(institution_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'in_app',
            status TEXT NOT NULL DEFAULT 'unread',
            reference_type TEXT,
            reference_id TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(institution_id, recipient_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS email_outbox (
            email_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            recipients_json TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_message_id TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            error TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_email_outbox_institution ON email_outbox(institution_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            command_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '[]',
            steps_json TEXT NOT NULL DEFAULT '[]',
            tool_names_json TEXT NOT NULL DEFAULT '[]',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_institution ON agent_runs(institution_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            arguments_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_approvals_institution ON approvals(institution_id, status, created_at)",
        """
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON background_jobs(status, created_at)",
    ])
    return tuple(statements)


def postgres_row_level_security() -> tuple[str, ...]:
    """Idempotent RLS statements; the connection role must not bypass RLS."""

    statements: list[str] = []
    for table in TENANT_TABLES:
        policy = f"{table}_tenant_isolation"
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        statements.append(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = '{table}' AND policyname = '{policy}') THEN "
            f"CREATE POLICY {policy} ON {table} "
            "USING (institution_id = current_setting('app.institution_id', true)) "
            "WITH CHECK (institution_id = current_setting('app.institution_id', true)); "
            "END IF; END $$"
        )
    return tuple(statements)


# Columns added after a table first shipped; ``CREATE TABLE IF NOT EXISTS`` does
# not add them to an existing table, so the store adds each one when missing.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("generated_reports", "required_capabilities_json", "TEXT"),
)


def postgres_numeric_columns() -> frozenset[tuple[str, str]]:
    """(table, column) pairs that must be DOUBLE PRECISION on PostgreSQL.

    Earlier schemas created these as REAL (float4), which makes ``SUM`` over
    money and percentages drift; the store widens any that are still ``real``.
    """

    return frozenset(
        (entity.table, item.name)
        for entity in CANONICAL_ENTITIES.values()
        for item in entity.fields
        if item.field_type in (FieldType.NUMBER, FieldType.PERCENT)
    )


def render_sql_migration() -> str:
    lines = [f"-- Guru Ji institution data schema {SCHEMA_VERSION}", "-- Generated from app.normalization.canonical; edit the model, not this file."]
    for statement in portable_statements():
        lines.append(" ".join(statement.split()) + ";")
    lines.append("-- PostgreSQL row-level security (skipped on SQLite)")
    for statement in postgres_row_level_security():
        lines.append(statement + ";")
    return "\n".join(lines) + "\n"


__all__ = [
    "LINEAGE_COLUMNS",
    "ADDED_COLUMNS",
    "SCHEMA_VERSION",
    "TENANT_TABLES",
    "column_type",
    "entity_table_ddl",
    "portable_statements",
    "postgres_numeric_columns",
    "postgres_row_level_security",
    "render_sql_migration",
]
