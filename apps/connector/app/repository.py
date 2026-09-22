"""Read-only institution reporting repository implementations.

The connector owns the institution boundary. It exposes aggregate semantic
records only; raw student records and write operations never cross this API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class ReportingRepository(Protocol):
    def health(self) -> dict[str, object]: ...

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        scope: dict[str, str | None],
        max_rows: int,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class DemoReportingRepository:
    """Deterministic aggregate data for local stack tests only."""

    source_id: str
    institution_id: str

    def health(self) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "status": "healthy",
            "freshness": "fresh",
            "last_success_at": now,
            "detail": "connector demo repository is available; no college system was contacted",
        }

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        scope: dict[str, str | None],
        max_rows: int,
    ) -> dict[str, object]:
        del arguments
        now = datetime.now(timezone.utc)
        period = {
            "started_at": now.replace(month=1, day=1).isoformat(),
            "ended_at": now.isoformat(),
        }
        if tool_name == "institution.overview":
            data: dict[str, object] = {
                "institution_id": self.institution_id,
                "college_id": scope["college_id"],
                "active_students": 1240,
                "active_departments": 3,
                "attendance_rate_percent": 87.4,
                "reporting_period": period,
            }
        elif tool_name == "institution.attendance_summary":
            rows = [
                {"department_id": "dept-cse", "name": "Computer Science", "attendance_rate_percent": 89.1},
                {"department_id": "dept-ece", "name": "Electronics", "attendance_rate_percent": 85.8},
                {"department_id": "dept-me", "name": "Mechanical", "attendance_rate_percent": 83.6},
            ]
            if scope.get("department_id"):
                rows = [row for row in rows if row["department_id"] == scope["department_id"]]
            data = {
                "college_id": scope["college_id"],
                "department_id": scope.get("department_id"),
                "attendance_rate_percent": 87.4 if not scope.get("department_id") else (rows[0]["attendance_rate_percent"] if rows else None),
                "departments": rows[:max_rows],
                "reporting_period": period,
            }
        elif tool_name == "institution.source_health":
            data = {"source_id": self.source_id, **self.health()}
        else:
            raise KeyError(tool_name)
        return data


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class PostgresReportingRepository:
    """Read-only adapter over institution-owned reporting views.

    The deployment must grant this connector a database role with SELECT-only
    access to the configured views. Identifiers are validated before being
    interpolated; all scope and row-limit values remain bound parameters.
    """

    database_url: str
    source_id: str
    institution_id: str
    schema: str = "public"
    overview_view: str = "guru_student_overview"
    attendance_view: str = "guru_attendance_summary"
    connect_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        for name, value in (("schema", self.schema), ("overview_view", self.overview_view), ("attendance_view", self.attendance_view)):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is not a safe SQL identifier")
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("connector database URL must be PostgreSQL")

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency is project-managed
            raise RuntimeError("psycopg is required for the PostgreSQL connector") from exc
        return psycopg.connect(
            self.database_url,
            connect_timeout=self.connect_timeout_seconds,
            autocommit=True,
            row_factory=dict_row,
        )

    def health(self) -> dict[str, object]:
        started = datetime.now(timezone.utc)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return {
                "status": "healthy",
                "freshness": "fresh",
                "last_success_at": started.isoformat(),
                "detail": "read-only reporting views are reachable",
            }
        except Exception:  # noqa: BLE001 - health must not leak driver details
            return {
                "status": "unavailable",
                "freshness": "unknown",
                "detail": "read-only reporting database is unavailable",
            }

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        scope: dict[str, str | None],
        max_rows: int,
    ) -> dict[str, object]:
        del arguments
        if tool_name == "institution.source_health":
            return {"source_id": self.source_id, **self.health()}
        view = self.overview_view if tool_name == "institution.overview" else self.attendance_view if tool_name == "institution.attendance_summary" else None
        if view is None:
            raise KeyError(tool_name)
        columns = (
            "active_students, active_departments, attendance_rate_percent"
            if tool_name == "institution.overview"
            else "department_id, department_name, attendance_rate_percent"
        )
        predicates = ["college_id = %s"]
        values: list[object] = [scope["college_id"]]
        if scope.get("department_id") is not None:
            predicates.append("department_id = %s")
            values.append(scope["department_id"])
        if scope.get("batch_id") is not None:
            predicates.append("batch_id = %s")
            values.append(scope["batch_id"])
        values.append(max(1, min(max_rows, 1_000)))
        query = f'SELECT {columns} FROM "{self.schema}"."{view}" WHERE ' + " AND ".join(predicates) + " LIMIT %s"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, values)
            rows = [dict(row) for row in cursor.fetchall()]
        return {"college_id": scope["college_id"], "department_id": scope.get("department_id"), "rows": rows}


__all__ = ["DemoReportingRepository", "PostgresReportingRepository", "ReportingRepository"]
