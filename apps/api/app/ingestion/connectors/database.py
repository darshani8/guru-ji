"""Read rows from an institution's existing database into the intermediate model.

The connector only ever runs ``SELECT`` against an explicitly named table or
approved view using a read-only credential. Column names become headers, so
the same mapping engine applies as for a spreadsheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ...connectors.common.sql_safety import validate_identifier
from ..models import FileKind, IntermediateRecord, ParseResult, ParsedTable, ParserError
from ..parsers.tabular import MAX_ROWS, dedupe_headers

_ALLOWED_SCHEMES = {"postgresql", "postgres", "mysql", "mysql+pymysql", "mssql", "mssql+pyodbc", "oracle", "sqlite"}


@dataclass(slots=True)
class ExistingDatabaseConnector:
    database_url: str = field(repr=False)
    max_rows: int = MAX_ROWS
    connect_timeout_seconds: float = 10.0
    engine: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.database_url)
        scheme = parsed.scheme.split("+")[0]
        if parsed.scheme not in _ALLOWED_SCHEMES and scheme not in _ALLOWED_SCHEMES:
            raise ValueError("unsupported database scheme for ingestion")

    def _engine(self) -> Any:
        if self.engine is not None:
            return self.engine
        from sqlalchemy import create_engine

        options: dict[str, Any] = {"pool_pre_ping": True}
        if self.database_url.startswith(("postgresql", "postgres")):
            options["connect_args"] = {"connect_timeout": int(self.connect_timeout_seconds), "options": "-c default_transaction_read_only=on"}
        self.engine = create_engine(self.database_url, **options)
        return self.engine

    def list_tables(self) -> list[str]:
        from sqlalchemy import inspect

        inspector = inspect(self._engine())
        names = list(inspector.get_table_names()) + list(inspector.get_view_names())
        return sorted(dict.fromkeys(names))

    def fetch_table(self, table: str, *, schema: str | None = None, limit: int | None = None) -> ParseResult:
        from sqlalchemy import text

        validate_identifier(table)
        if schema is not None:
            validate_identifier(schema)
        qualified = f"{schema}.{table}" if schema else table
        row_limit = min(limit or self.max_rows, self.max_rows)
        statement = text(f"SELECT * FROM {qualified}")  # noqa: S608 - identifiers validated above
        rows: list[dict[str, Any]] = []
        try:
            with self._engine().connect() as connection:
                if hasattr(connection, "execution_options"):
                    connection = connection.execution_options(stream_results=True)
                cursor = connection.execute(statement)
                for row in cursor.mappings():
                    rows.append(dict(row))
                    if len(rows) >= row_limit:
                        break
        except Exception as exc:  # noqa: BLE001 - driver-specific errors are not safe to expose
            raise ParserError(f"table {qualified} could not be read with the read-only credential") from exc
        headers = dedupe_headers(list(rows[0].keys())) if rows else ()
        records = [
            IntermediateRecord(source_file=f"db:{qualified}", locator=f"table={qualified};row={index}", row_number=index, fields={key: row.get(key) for key in headers})
            for index, row in enumerate(rows, start=1)
        ]
        result = ParseResult(file_name=f"db:{qualified}", file_kind=FileKind.CSV, page_count=1, metadata={"source": "database", "table": qualified})
        result.tables.append(ParsedTable(name=qualified, headers=headers, records=records, warnings=["row_limit_reached"] if len(rows) >= row_limit else []))
        return result


__all__ = ["ExistingDatabaseConnector"]
