"""Start-up locking and transaction hygiene of the PostgreSQL stores, with fake drivers.

A rolling deployment starts the new release while the previous one is still
serving on the same database. Start-up must therefore never queue for an
ACCESS EXCLUSIVE lock behind the old instance's connections, and no store
may leave a transaction open between calls (which is what held those locks).
"""

from __future__ import annotations

import sys
import types
import unittest

from app.institution_data.schema import ADDED_COLUMNS, TENANT_TABLES, portable_statements
from app.institution_data.store import InstitutionDataStore
from app.internet_intelligence.store import _STATEMENTS as INTELLIGENCE_STATEMENTS, _TENANT_TABLES as INTELLIGENCE_TENANT_TABLES, IntelligenceStore
from app.persistence.database import PostgresControlStore
from app.persistence.schema_tools import _INDEX_STATEMENT
from app.persistence.sql_backend import SqlBackend


def _flat(sql: str) -> str:
    return " ".join(sql.split())


class _Info:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    @property
    def transaction_status(self) -> int:
        return self._connection.status


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self._rows: list[dict] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        connection = self.connection
        statement = _flat(sql)
        connection.statements.append((statement, tuple(params)))
        if not connection.autocommit and connection.status == 0:
            connection.status = 2  # the driver's implicit BEGIN
        self._rows = []
        if "information_schema.columns" in statement:
            self._rows = [{"present": 1}] if (params[0], params[1]) in connection.columns else []
        elif "pg_indexes" in statement:
            self._rows = [{"present": 1}] if params[0] in connection.indexes else []
        elif statement.startswith("CREATE TABLE IF NOT EXISTS "):
            table = statement.split()[5]
            if table not in connection.tables:
                connection.tables.add(table)
                connection.columns.update((t, c) for t, c, _ in PostgresControlStore._ADDED_COLUMNS if t == table)
        elif statement.startswith("ALTER TABLE "):
            words = statement.split()
            connection.columns.add((words[2], words[8]))
        elif statement.startswith("CREATE INDEX IF NOT EXISTS "):
            connection.indexes.add(statement.split()[5])

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def __enter__(self) -> "_Transaction":
        self.connection.depth += 1
        self.connection.status = 2
        self.connection.transactions += 1
        return self

    def __exit__(self, exc_type: object, *exc: object) -> bool:
        self.connection.depth -= 1
        if self.connection.depth == 0:
            self.connection.status = 0
            if exc_type is None:
                self.connection.commits += 1
            else:
                self.connection.rollbacks += 1
        return False


class _Connection:
    def __init__(self, autocommit: bool) -> None:
        self.autocommit = autocommit
        self.status = 0  # libpq: 0 IDLE, 2 INTRANS
        self.depth = 0
        self.statements: list[tuple[str, tuple]] = []
        self.tables: set[str] = set()
        self.columns: set[tuple[str, str]] = set()
        self.indexes: set[str] = set()
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0
        self.explicit_commits = 0
        self.closed = False
        self.info = _Info(self)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def commit(self) -> None:
        self.explicit_commits += 1

    def close(self) -> None:
        self.closed = True


class _FakePsycopg(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("psycopg")
        self.OperationalError = type("OperationalError", (Exception,), {})
        self.reject_session_options = False
        self.connections: list[_Connection] = []
        self.rows = types.ModuleType("psycopg.rows")
        self.rows.dict_row = object()
        self.migrated = False

    def connect(self, url: str, row_factory: object = None, autocommit: bool = False, **options: object) -> _Connection:
        if self.reject_session_options and "options" in options:
            raise self.OperationalError('connection failed: unrecognized configuration parameter "client_connection_check_interval"')
        self.connect_options = dict(options)
        connection = _Connection(autocommit)
        if self.migrated:
            connection.tables = {"schema_migrations", "audit_events", "source_health", "briefing_runs", "answer_envelopes", "model_attempts", "control_outbox"}
            connection.columns = {(table, column) for table, column, _ in PostgresControlStore._ADDED_COLUMNS}
            connection.indexes = {index for index, _ in PostgresControlStore._INDEXES}
        self.connections.append(connection)
        return connection


class PostgresControlStoreStartUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _FakePsycopg()
        self._saved = {name: sys.modules.get(name) for name in ("psycopg", "psycopg.rows")}
        sys.modules["psycopg"] = self.fake
        sys.modules["psycopg.rows"] = self.fake.rows

    def tearDown(self) -> None:
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _ddl(self, connection: _Connection) -> list[str]:
        return [sql for sql, _ in connection.statements if sql.startswith(("ALTER TABLE", "CREATE INDEX"))]

    def test_a_migrated_database_gets_no_table_locking_ddl(self) -> None:
        self.fake.migrated = True
        store = PostgresControlStore("postgresql://fake/db")
        connection = self.fake.connections[0]
        self.assertTrue(connection.autocommit, "reads must not leave the connection idle in transaction")
        self.assertEqual(self.fake.connect_options.get("connect_timeout"), 15, "a silent TCP connect must fail within seconds")
        self.assertEqual(self.fake.connect_options.get("options"), "-c client_connection_check_interval=10s", "a backend whose client died must not wait for a lock forever")
        self.assertEqual(self._ddl(connection), [], "ALTER TABLE / CREATE INDEX lock the table even when they change nothing")
        self.assertEqual(connection.statements[0][0], "SET LOCAL lock_timeout = '15s'", "a blocked start-up fails loudly instead of hanging")
        self.assertEqual((connection.transactions, connection.commits, connection.rollbacks), (1, 1, 0))
        self.assertEqual(connection.explicit_commits, 0)
        self.assertFalse(store.in_transaction)
        self.assertIn("INSERT INTO schema_migrations", connection.statements[-1][0])

    def test_a_server_without_the_client_check_is_connected_without_it(self) -> None:
        self.fake.migrated = True
        self.fake.reject_session_options = True
        PostgresControlStore("postgresql://fake/db")
        self.assertEqual(len(self.fake.connections), 1)
        self.assertNotIn("options", self.fake.connect_options)
        self.assertEqual(self.fake.connect_options.get("connect_timeout"), 15)

    def test_pruning_waits_for_locks_only_briefly(self) -> None:
        self.fake.migrated = True
        store = PostgresControlStore("postgresql://fake/db")
        connection = self.fake.connections[0]
        start = len(connection.statements)
        store.prune_retention(30)
        self.assertEqual(connection.statements[start][0], "SET LOCAL lock_timeout = '15s'")

    def test_only_what_the_catalog_lacks_is_added(self) -> None:
        self.fake.migrated = True
        original_connect = self.fake.connect

        def connect(url: str, row_factory: object = None, autocommit: bool = False, **options: object) -> _Connection:
            connection = original_connect(url, row_factory, autocommit, **options)
            connection.columns.discard(("source_health", "last_success_at"))
            connection.indexes.discard("idx_control_outbox_pending")
            return connection

        self.fake.connect = connect  # type: ignore[method-assign]
        PostgresControlStore("postgresql://fake/db")
        self.assertEqual(
            self._ddl(self.fake.connections[0]),
            [
                "ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_success_at TEXT",
                "CREATE INDEX IF NOT EXISTS idx_control_outbox_pending ON control_outbox(delivered_at, created_at)",
            ],
        )

    def test_a_fresh_database_is_created_in_one_transaction_without_alters(self) -> None:
        PostgresControlStore("postgresql://fake/db")
        connection = self.fake.connections[0]
        self.assertEqual([sql for sql in self._ddl(connection) if sql.startswith("ALTER")], [], "CREATE TABLE already has every column")
        self.assertEqual(len([sql for sql in self._ddl(connection) if sql.startswith("CREATE INDEX")]), len(PostgresControlStore._INDEXES))
        self.assertEqual((connection.transactions, connection.commits), (1, 1))

    def test_reads_and_pings_leave_no_transaction_open(self) -> None:
        self.fake.migrated = True
        store = PostgresControlStore("postgresql://fake/db")
        connection = self.fake.connections[0]
        self.assertEqual(store.recent_audit(5), ())
        self.assertEqual(store.all_health(), ())
        self.assertEqual(store.drain_outbox(), ())
        self.assertEqual(store.recent_briefings(), ())
        self.assertIsNone(store.get_health("source-1"))
        self.assertTrue(store.ping())
        self.assertFalse(store.in_transaction)
        self.assertEqual(connection.status, 0)
        self.assertEqual(connection.explicit_commits, 0)

    def test_multi_statement_writes_run_in_one_transaction(self) -> None:
        self.fake.migrated = True
        store = PostgresControlStore("postgresql://fake/db")
        connection = self.fake.connections[0]
        before = connection.transactions
        store.ack_outbox(("outbox-a", "outbox-b"))
        self.assertEqual(connection.transactions, before + 1)
        self.assertEqual(len([sql for sql, _ in connection.statements if sql.startswith("UPDATE control_outbox")]), 2)
        store.prune_retention(30)
        self.assertEqual(connection.transactions, before + 2)
        self.assertEqual(connection.commits, before + 2)
        self.assertFalse(store.in_transaction)
        self.assertEqual(connection.explicit_commits, 0)


def _index_names(statements) -> set[str]:
    names = set()
    for statement in statements:
        match = _INDEX_STATEMENT.match(statement)
        if match is not None:
            names.add(match.group(1))
    return names


class _CatalogBackend(SqlBackend):
    """A PostgreSQL-dialect backend that answers catalog queries and records statements.

    ``migrated`` describes a database an earlier release already brought up
    to date: every table, column, index and tenant policy exists, tenant
    tables enforce row-level security and institution_profiles does not.
    """

    dialect = "postgresql"

    def __init__(self, *, migrated: bool = True) -> None:
        super().__init__()
        self.migrated = migrated
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.indexes = _index_names(portable_statements()) | _index_names(INTELLIGENCE_STATEMENTS) if migrated else set()
        tenant_tables = tuple(TENANT_TABLES) + tuple(INTELLIGENCE_TENANT_TABLES)
        self.tables = {table: (True, True) for table in tenant_tables} if migrated else {}
        if migrated:
            self.tables["institution_profiles"] = (False, False)
        self.policies = {(table, f"{table}_tenant_isolation") for table in tenant_tables} if migrated else set()

    def execute(self, sql: str, params=()) -> int:
        statement = _flat(sql)
        self.statements.append(statement)
        match = _INDEX_STATEMENT.match(statement)
        if match is not None:
            self.indexes.add(match.group(1))
        return 0

    def executemany(self, sql: str, rows) -> int:
        self.statements.append(_flat(sql))
        return 0

    def fetchall(self, sql: str, params=()) -> list[dict]:
        statement = _flat(sql)
        self.statements.append(statement)
        if "information_schema.columns" in statement and "data_type = 'real'" in statement:
            return []
        if "information_schema.columns" in statement:
            return [{"present": 1}] if self.migrated else []
        if "pg_indexes" in statement:
            return [{"indexname": name} for name in sorted(self.indexes)]
        if "pg_policies" in statement:
            return [{"tablename": table, "policyname": policy} for table, policy in sorted(self.policies)]
        if "pg_class" in statement:
            return [{"relname": table, "relrowsecurity": enabled, "relforcerowsecurity": forced} for table, (enabled, forced) in sorted(self.tables.items())]
        return []

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


_LOCKING = ("ALTER TABLE", "CREATE INDEX", "CREATE POLICY", "DROP POLICY")


class InstitutionDataStoreStartUpTests(unittest.TestCase):
    def test_a_migrated_database_gets_no_locking_ddl(self) -> None:
        backend = _CatalogBackend()
        InstitutionDataStore(backend=backend)
        self.assertEqual(backend.statements[0], "SET LOCAL lock_timeout = '15s'")
        self.assertEqual([sql for sql in backend.statements if sql.startswith(_LOCKING)], [], "ALTER TABLE, CREATE INDEX and CREATE POLICY lock the table even when they change nothing")
        self.assertEqual((backend.commits, backend.rollbacks), (1, 0), "the whole migration is one transaction")
        self.assertFalse(backend.in_transaction)
        self.assertEqual(len([sql for sql in backend.statements if "pg_indexes" in sql]), 1, "one catalog query covers every index")

    def test_a_fresh_database_gets_every_index_column_and_policy_in_one_transaction(self) -> None:
        backend = _CatalogBackend(migrated=False)
        InstitutionDataStore(backend=backend)
        alters = [sql for sql in backend.statements if sql.startswith("ALTER TABLE")]
        for table, column, column_type in ADDED_COLUMNS:
            self.assertIn(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}", alters)
        for table in TENANT_TABLES:
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", alters)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", alters)
            self.assertTrue(any(sql.startswith(f"CREATE POLICY {table}_tenant_isolation ON {table} ") for sql in backend.statements))
        created = {sql.split()[5] for sql in backend.statements if sql.startswith("CREATE INDEX IF NOT EXISTS")}
        self.assertEqual(created, _index_names(portable_statements()))
        self.assertEqual((backend.commits, backend.rollbacks), (1, 0))

    def test_only_the_missing_part_of_row_level_security_is_applied(self) -> None:
        backend = _CatalogBackend()
        backend.tables["students"] = (True, False)  # enabled but not forced
        backend.policies.discard(("students", "students_tenant_isolation"))
        InstitutionDataStore(backend=backend)
        locking = [sql for sql in backend.statements if sql.startswith(_LOCKING)]
        self.assertEqual(locking[0], "ALTER TABLE students FORCE ROW LEVEL SECURITY")
        self.assertTrue(locking[1].startswith("CREATE POLICY students_tenant_isolation ON students "))
        self.assertEqual(len(locking), 2)


class IntelligenceStoreStartUpTests(unittest.TestCase):
    def test_a_migrated_database_gets_no_locking_ddl(self) -> None:
        backend = _CatalogBackend()
        IntelligenceStore(backend=backend)
        self.assertEqual(backend.statements[0], "SET LOCAL lock_timeout = '15s'")
        self.assertEqual([sql for sql in backend.statements if sql.startswith(_LOCKING)], [])
        self.assertEqual((backend.commits, backend.rollbacks), (1, 0))
        self.assertIn("INSERT INTO schema_migrations", backend.statements[-1])

    def test_a_fresh_database_gets_indexes_and_tenant_isolation(self) -> None:
        backend = _CatalogBackend(migrated=False)
        IntelligenceStore(backend=backend)
        alters = [sql for sql in backend.statements if sql.startswith("ALTER TABLE")]
        for table in INTELLIGENCE_TENANT_TABLES:
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", alters)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", alters)
        self.assertNotIn("ALTER TABLE institution_profiles DISABLE ROW LEVEL SECURITY", alters, "nothing to undo on a fresh database")
        created = {sql.split()[5] for sql in backend.statements if sql.startswith("CREATE INDEX IF NOT EXISTS")}
        self.assertEqual(created, _index_names(INTELLIGENCE_STATEMENTS))

    def test_a_database_that_still_forces_profiles_isolation_is_converged(self) -> None:
        backend = _CatalogBackend()
        backend.tables["institution_profiles"] = (True, True)
        backend.policies.add(("institution_profiles", "institution_profiles_tenant_isolation"))
        IntelligenceStore(backend=backend)
        self.assertEqual(
            [sql for sql in backend.statements if sql.startswith(_LOCKING)],
            [
                "ALTER TABLE institution_profiles NO FORCE ROW LEVEL SECURITY",
                "ALTER TABLE institution_profiles DISABLE ROW LEVEL SECURITY",
                "DROP POLICY IF EXISTS institution_profiles_tenant_isolation ON institution_profiles",
            ],
        )


if __name__ == "__main__":
    unittest.main()
