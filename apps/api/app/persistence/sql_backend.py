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
            try:
                if tenant_id is not None:
                    self.set_tenant(tenant_id)
                yield
                self.commit()
            except Exception:
                self.rollback()
                raise

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
        self._connection = psycopg.connect(database_url, row_factory=dict_row)

    def _adapt(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(self._adapt(sql), tuple(params))
            return cursor.rowcount if cursor.rowcount is not None else 0

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return 0
        with self._lock, self._connection.cursor() as cursor:
            cursor.executemany(self._adapt(sql), materialized)
            return len(materialized)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(self._adapt(sql), tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def set_tenant(self, tenant_id: str) -> None:
        # Row-level security policies read app.institution_id; SET LOCAL binds it
        # to the current transaction only, so no tenant leaks into the next one.
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.institution_id', %s, true)", (tenant_id,))

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self._lock:
            self._connection.rollback()

    def ping(self) -> bool:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True


def open_backend(database_url: str | None) -> SqlBackend:
    if not database_url or database_url.startswith("sqlite://") or database_url == ":memory:":
        return SqliteBackend(database_url or ":memory:")
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(database_url)
    raise ValueError("database URL must use sqlite://, postgresql://, or postgres://")


__all__ = ["PostgresBackend", "SqlBackend", "SqliteBackend", "open_backend"]
