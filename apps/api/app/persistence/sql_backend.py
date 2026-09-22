"""Small DB-API adapter shared by the institution-data and intelligence stores.

Statements are written once with ``?`` placeholders. The PostgreSQL backend
rewrites them to ``%s`` so every store method has exactly one SQL string and
one code path per operation. Both backends return plain dictionaries.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SqlBackend:
    dialect: str

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._transaction_depth = 0
        # Statements run so far in the current outermost transaction, and its
        # tenant; the PostgreSQL backend uses both to decide whether a dropped
        # connection can be reopened and the statement retried safely.
        self._transaction_statements = 0
        self._transaction_tenant: str | None = None

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    # -- lifecycle -----------------------------------------------------------------
    def close(self) -> None:
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError

    # -- statements ----------------------------------------------------------------
    def _adapt(self, sql: str) -> str:
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise NotImplementedError

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        raise NotImplementedError

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def executescript(self, statements: Iterable[str]) -> None:
        with self.transaction():
            for statement in statements:
                if statement.strip():
                    self.execute(statement)

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    @contextmanager
    def transaction(self, tenant_id: str | None = None) -> Iterator[None]:
        """Run a block atomically; PostgreSQL additionally pins the tenant for RLS."""

        with self._lock:
            self._transaction_depth += 1
            if self._transaction_depth == 1:
                self._transaction_statements = 0
                self._transaction_tenant = tenant_id
            try:
                if tenant_id is not None:
                    self.set_tenant(tenant_id)
                yield
                if self._transaction_depth == 1:
                    self.commit()
            except Exception:
                if self._transaction_depth == 1:
                    self.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def set_tenant(self, tenant_id: str) -> None:
        return None


class SqliteBackend(SqlBackend):
    dialect = "sqlite"

    def __init__(self, database_url: str) -> None:
        super().__init__()
        self.path = self._path_from_url(database_url)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(":memory:" if self.path is None else str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _path_from_url(database_url: str) -> Path | None:
        if database_url in {":memory:", "sqlite://:memory:", "sqlite:///:memory:"} or database_url.endswith("/:memory:"):
            return None
        if not database_url.startswith("sqlite://"):
            raise ValueError("sqlite backend requires a sqlite:// URL")
        raw = database_url.removeprefix("sqlite://")
        if raw.startswith("/"):
            raw = raw[1:]
        if raw.startswith("./"):
            return Path.cwd() / raw[2:]
        return Path(raw).expanduser().resolve()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            return cursor.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._lock:
            cursor = self._connection.executemany(sql, [tuple(row) for row in rows])
            return cursor.rowcount

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self._lock:
            self._connection.rollback()

    def ping(self) -> bool:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True


class PostgresBackend(SqlBackend):
    """One psycopg connection that is reopened when the server drops it.

    A statement that fails because the connection broke (server restart,
    ``pg_terminate_backend``, idle timeouts) is retried once on a fresh
    connection when nothing else has run in the current transaction: outside
    an explicit transaction, or on the first statement of one (the tenant
    setting is re-applied first). Once a transaction has executed a statement
    the connection is reopened and the error is re-raised so the caller's
    transaction fails cleanly instead of half-applying.
    """

    dialect = "postgresql"

    def __init__(self, database_url: str) -> None:
        super().__init__()
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgresBackend requires a postgresql:// or postgres:// URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised only without psycopg
            raise RuntimeError("PostgreSQL support requires the psycopg[binary] dependency") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._database_url = database_url
        self._connection: Any = None
        self._connection = self._open_connection()

    # -- connection management --------------------------------------------------
    def _open_connection(self) -> Any:
        return self._psycopg.connect(self._database_url, row_factory=self._dict_row)

    @staticmethod
    def _is_broken(connection: Any) -> bool:
        return connection is None or bool(getattr(connection, "closed", False)) or bool(getattr(connection, "broken", False))

    def _discard_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - a dead socket may refuse to close cleanly
                pass

    def _ensure_connection(self) -> Any:
        """Return a usable connection, reopening it when the previous one broke."""

        if self._closed:
            raise self._psycopg.InterfaceError("the backend has been closed")
        if self._is_broken(self._connection):
            self._discard_connection()
            self._connection = self._open_connection()
        return self._connection

    def _run(self, operation: Any, *, counts: bool = True) -> Any:
        """Run ``operation(connection)`` with reconnect-and-retry on a dropped connection."""

        with self._lock:
            connection = self._ensure_connection()
            try:
                result = operation(connection)
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                if not self._is_broken(connection):
                    # A genuine statement failure (cancel, timeout, ...) on a live connection.
                    self._rollback_outside_transaction(connection)
                    raise
                self._discard_connection()
                if self.in_transaction and self._transaction_statements > 0:
                    # The server already aborted a transaction that had done
                    # work; reopen so the next transaction works, and let this
                    # one fail cleanly rather than replaying part of it.
                    try:
                        self._connection = self._open_connection()
                    except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                        pass  # still down: the next statement reopens it
                    raise
                connection = self._ensure_connection()
                if counts and self.in_transaction and self._transaction_tenant is not None:
                    # A real statement inside a tenant transaction: re-pin the
                    # tenant on the fresh connection before replaying it.
                    self._apply_tenant(connection, self._transaction_tenant)
                result = operation(connection)
            except Exception:
                # A failed statement (bad SQL, constraint, division by zero)
                # leaves the driver's implicit transaction aborted; outside an
                # explicit transaction nobody else would roll it back.
                self._rollback_outside_transaction(connection)
                raise
            if counts and self.in_transaction:
                self._transaction_statements += 1
            return result

    def _rollback_outside_transaction(self, connection: Any) -> None:
        if self.in_transaction or self._is_broken(connection):
            return
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001 - the connection is unusable; drop it
            self._discard_connection()

    @staticmethod
    def _apply_tenant(connection: Any, tenant_id: str) -> None:
        # Row-level security policies read app.institution_id; SET LOCAL binds it
        # to the current transaction only, so no tenant leaks into the next one.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.institution_id', %s, true)", (tenant_id,))

    def _adapt(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def _autocommit_if_outside_transaction(self, connection: Any) -> None:
        # A statement outside an explicit transaction must not leave the
        # connection idle-in-transaction (and holding a tenant setting).
        if not self.in_transaction:
            connection.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        statement = self._adapt(sql)
        values = tuple(params)

        def operation(connection: Any) -> int:
            with connection.cursor() as cursor:
                cursor.execute(statement, values)
                affected = cursor.rowcount if cursor.rowcount is not None else 0
            self._autocommit_if_outside_transaction(connection)
            return affected

        return self._run(operation)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return 0
        statement = self._adapt(sql)

        def operation(connection: Any) -> int:
            with connection.cursor() as cursor:
                cursor.executemany(statement, materialized)
            self._autocommit_if_outside_transaction(connection)
            return len(materialized)

        return self._run(operation)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        statement = self._adapt(sql)
        values = tuple(params)

        def operation(connection: Any) -> list[dict[str, Any]]:
            with connection.cursor() as cursor:
                cursor.execute(statement, values)
                rows = [dict(row) for row in cursor.fetchall()]
            self._autocommit_if_outside_transaction(connection)
            return rows

        return self._run(operation)

    def set_tenant(self, tenant_id: str) -> None:
        # Idempotent, so it does not count as work done in the transaction: a
        # retry on a fresh connection re-applies it before the real statement.
        self._run(lambda connection: self._apply_tenant(connection, tenant_id), counts=False)

    def commit(self) -> None:
        self._run(lambda connection: connection.commit())

    def rollback(self) -> None:
        with self._lock:
            connection = self._connection
            if self._is_broken(connection):
                # The server already discarded the transaction with the connection.
                self._discard_connection()
                return
            try:
                connection.rollback()
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                self._discard_connection()

    def ping(self) -> bool:
        def operation(connection: Any) -> bool:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            # Readiness probes run outside a transaction; commit so the probe
            # never leaves the connection idle-in-transaction.
            self._autocommit_if_outside_transaction(connection)
            return True

        return bool(self._run(operation))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._discard_connection()
                self._closed = True


def open_backend(database_url: str | None) -> SqlBackend:
    if not database_url or database_url.startswith("sqlite://") or database_url == ":memory:":
        return SqliteBackend(database_url or ":memory:")
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(database_url)
    raise ValueError("database URL must use sqlite://, postgresql://, or postgres://")


__all__ = ["PostgresBackend", "SqlBackend", "SqliteBackend", "open_backend"]
