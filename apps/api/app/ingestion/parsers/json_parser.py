"""JSON exports (a list of objects, or an object holding one) and plain text."""

from __future__ import annotations

import json
from typing import Any

from ..models import FileKind, IntermediateRecord, ParseResult, ParsedTable, ParsedText, ParserError
from .csv_parser import decode_text
from .tabular import MAX_ROWS, dedupe_headers


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for candidate in value.values():
            if isinstance(candidate, list) and candidate and all(isinstance(item, dict) for item in candidate[:10]):
                return [item for item in candidate if isinstance(item, dict)]
        return [value]
    return []


def parse_json(file_name: str, content: bytes) -> ParseResult:
    text, _ = decode_text(content)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ParserError("JSON could not be decoded") from exc
    rows = _rows(payload)[:MAX_ROWS]
    result = ParseResult(file_name=file_name, file_kind=FileKind.JSON, page_count=1)
    if not rows:
        result.warnings.append("no_object_rows_found")
        return result
    headers = dedupe_headers(list(dict.fromkeys(key for row in rows for key in row)))
    records = [
        IntermediateRecord(source_file=file_name, locator=f"json;index={index}", row_number=index, fields={key: row.get(key) for key in headers})
        for index, row in enumerate(rows, start=1)
    ]
    result.tables.append(ParsedTable(name="data", headers=headers, records=records))
    return result


def parse_text(file_name: str, content: bytes) -> ParseResult:
    text, encoding = decode_text(content)
    result = ParseResult(file_name=file_name, file_kind=FileKind.TEXT, page_count=1, metadata={"encoding": encoding})
    if text.strip():
        result.texts.append(ParsedText(locator="body", text=text, page=1))
    return result


__all__ = ["parse_json", "parse_text"]
