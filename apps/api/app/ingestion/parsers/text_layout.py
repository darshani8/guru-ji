"""Turn OCR or plain text into rows when it looks like a table, else keep it as text."""

from __future__ import annotations

import re
from typing import Any

from ..models import ParsedTable
from .tabular import grid_to_table

_SPLIT = re.compile(r"\s{2,}|\t|\s\|\s|,")
_KEY_VALUE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./'()-]{1,60}?)\s*[:\-]\s+(.+?)\s*$")


def text_to_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cells = [cell.strip() for cell in _SPLIT.split(stripped) if cell.strip()]
        rows.append(cells)
    return rows


def looks_tabular(rows: list[list[str]]) -> bool:
    widths = [len(row) for row in rows if len(row) > 1]
    if len(widths) < 3:
        return False
    common = max(set(widths), key=widths.count)
    return common >= 2 and widths.count(common) >= max(3, int(len(widths) * 0.6))


def key_value_fields(text: str) -> dict[str, Any]:
    """Extract ``Label: value`` pairs from a form-like page (admission forms, certificates)."""

    fields: dict[str, Any] = {}
    for line in text.splitlines():
        match = _KEY_VALUE.match(line)
        if not match:
            continue
        key = " ".join(match.group(1).split())
        value = match.group(2).strip()
        if key and value and key.lower() not in fields:
            fields[key] = value
    return fields


def text_to_table(text: str, *, name: str, source_file: str, page: int | None, ocr: bool, ocr_confidence: float | None) -> ParsedTable | None:
    rows = text_to_rows(text)
    if not looks_tabular(rows):
        return None
    table = grid_to_table(rows, name=name, source_file=source_file, page=page, ocr=ocr, ocr_confidence=ocr_confidence)
    return table if table.headers and table.records else None


__all__ = ["key_value_fields", "looks_tabular", "text_to_rows", "text_to_table"]
