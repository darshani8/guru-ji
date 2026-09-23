"""Start-up schema application that never waits on a lock it does not need.

The previous release keeps serving on the same PostgreSQL database while a new
one starts, so start-up may only issue DDL for what is actually missing:

* ``CREATE TABLE IF NOT EXISTS`` skips an existing table without locking it.
* ``CREATE INDEX IF NOT EXISTS`` takes a SHARE lock on the table even when the
  index exists, which waits behind the serving release's writes (an import in
  flight) and then blocks them; the catalog is consulted first instead.
* ``ALTER TABLE`` needs an ACCESS EXCLUSIVE lock even when it changes nothing,
  so row-level security is only enabled, forced or dropped where the catalog
  shows it is not already in that state.

Every helper runs inside the caller's transaction, which sets ``lock_timeout``
so a wait that does happen fails loudly instead of hanging the health check.
Start-up housekeeping (retention) lists tenants and bounds its lock waits
with the helpers at the end.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .sql_backend import SqlBackend

_INDEX_STATEMENT = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\b", re.IGNORECASE)

# How long start-up may wait for a table lock before failing loudly. A hang
# here is invisible (no log line, the health check never answers); an error
# reaches the container log.
MIGRATION_LOCK_TIMEOUT = "15s"


def begin_migration(backend: SqlBackend) -> None:
    """Bound lock waits for the current migration transaction (PostgreSQL only)."""

    if backend.dialect == "postgresql":
        backend.execute(f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'")


def existing_indexes(backend: SqlBackend) -> frozenset[str]:
    rows = backend.fetchall("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    return frozenset(str(row["indexname"]) for row in rows)


def apply_schema(backend: SqlBackend, statements: Iterable[str]) -> None:
    """Run schema statements, skipping ``CREATE INDEX IF NOT EXISTS`` for indexes PostgreSQL already has."""

    present: frozenset[str] | None = None
    for statement in statements:
        if not statement.strip():
            continue
        match = _INDEX_STATEMENT.match(statement)
        if match is not None and backend.dialect == "postgresql":
            if present is None:
                present = existing_indexes(backend)
            if match.group(1) in present:
                continue
        backend.execute(statement)


def add_missing_columns(backend: SqlBackend, columns: Iterable[tuple[str, str, str]]) -> None:
    """Add columns introduced after a table first shipped to databases that predate them.

    ``ADD COLUMN IF NOT EXISTS`` locks the table even when the column is
    there, so PostgreSQL is asked first and the statement runs only for a
    column the catalog does not list.
    """

    for table, column, column_type in columns:
        if backend.dialect == "postgresql":
            present = backend.fetchone(
                "SELECT 1 AS present FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
                (table, column),
            )
            if present is None:
                backend.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")
            continue
        present_columns = {row["name"] for row in backend.fetchall(f"PRAGMA table_info({table})")}
        if column not in present_columns:
            backend.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def row_level_security_state(backend: SqlBackend) -> dict[str, tuple[bool, bool]]:
    """table -> (enabled, forced) for every ordinary table in the current schema."""

    rows = backend.fetchall(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relnamespace = current_schema()::regnamespace AND relkind = 'r'"
    )
    return {str(row["relname"]): (bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])) for row in rows}


def existing_policies(backend: SqlBackend) -> frozenset[tuple[str, str]]:
    rows = backend.fetchall("SELECT tablename, policyname FROM pg_policies WHERE schemaname = current_schema()")
    return frozenset((str(row["tablename"]), str(row["policyname"])) for row in rows)


def tenant_isolation_statements(tables: Iterable[str], *, state: Mapping[str, tuple[bool, bool]], policies: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """The ALTER TABLE / CREATE POLICY statements that bring ``tables`` under forced tenant isolation.

    Only what the catalog shows missing is emitted, so a converged database
    gets no statement at all.
    """

    known = set(policies)
    statements: list[str] = []
    for table in tables:
        policy = f"{table}_tenant_isolation"
        enabled, forced = state.get(table, (False, False))
        if not enabled:
            statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        if not forced:
            statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        if (table, policy) not in known:
            statements.append(
                f"CREATE POLICY {policy} ON {table} "
                "USING (institution_id = current_setting('app.institution_id', true)) "
                "WITH CHECK (institution_id = current_setting('app.institution_id', true))"
            )
    return tuple(statements)


# Tables that name institutions and stay outside row-level security, so a
# platform-level job can list tenants without a tenant context: the
# intelligence profiles the monitoring scheduler reads, and the institution
# registry. Either may be absent from a database (a store used on its own).
TENANT_REGISTRIES = ("institution_profiles", "institutions")


def known_tenants(backend: SqlBackend, tables: Iterable[str]) -> list[str]:
    """Every institution that may have rows in ``tables``, for platform maintenance such as retention.

    With no tenant set, SQLite (which has no row-level security) shows every
    row, so the tables list their own institutions. Forced row-level security
    shows a PostgreSQL session none of them, so there the list also takes every
    institution the registries outside it name; an institution in neither is
    reached only by naming it. Callers still act on each institution inside its
    own tenant transaction.
    """

    found: set[str] = set()
    with backend.transaction():
        for table in tables:
            found.update(str(row["institution_id"]) for row in backend.fetchall(f"SELECT DISTINCT institution_id FROM {table}"))
        if backend.dialect == "postgresql":
            present = row_level_security_state(backend)
            for table in TENANT_REGISTRIES:
                if table in present:
                    found.update(str(row["institution_id"]) for row in backend.fetchall(f"SELECT institution_id FROM {table}"))
    return sorted(item for item in found if item.strip())


def bound_lock_waits(backend: SqlBackend) -> None:
    """Bound lock waits for the current transaction (PostgreSQL only): housekeeping that meets a stuck lock fails, never hangs."""

    begin_migration(backend)


def idempotent_tenant_isolation_sql(tables: Iterable[str]) -> tuple[str, ...]:
    """Tenant isolation for a migration file applied by hand: safe to run again.

    PostgreSQL has no CREATE POLICY IF NOT EXISTS, so each policy is created
    inside a guard that checks pg_policies first; a database the application
    already migrated at start-up takes the file without error.
    """

    statements: list[str] = []
    for table in tables:
        policy = f"{table}_tenant_isolation"
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        statements.append(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = '{table}' AND policyname = '{policy}') THEN "
            f"CREATE POLICY {policy} ON {table} "
            "USING (institution_id = current_setting('app.institution_id', true)) "
            "WITH CHECK (institution_id = current_setting('app.institution_id', true)); "
            "END IF; END $$"
        )
    return tuple(statements)


__all__ = [
    "MIGRATION_LOCK_TIMEOUT",
    "TENANT_REGISTRIES",
    "add_missing_columns",
    "apply_schema",
    "begin_migration",
    "bound_lock_waits",
    "existing_indexes",
    "idempotent_tenant_isolation_sql",
    "existing_policies",
    "known_tenants",
    "row_level_security_state",
    "tenant_isolation_statements",
]
