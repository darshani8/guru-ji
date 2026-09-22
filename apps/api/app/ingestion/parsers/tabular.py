"""Shared header detection for grids coming from CSV, spreadsheets, and tables."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..models import IntermediateRecord, ParsedTable

MAX_ROWS = 100_000
MAX_COLUMNS = 200


def _clean_header(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").replace("\n", " ").split())
    return text.strip().strip(":").strip()


def _looks_like_header(row: Sequence[Any]) -> bool:
    cells = [_clean_header(cell) for cell in row]
    non_empty = [cell for cell in cells if cell]
    if len(non_empty) < 2:
        return False
    textual = sum(1 for cell in non_empty if not re.fullmatch(r"[-+]?\d+(\.\d+)?%?", cell))
    # Duplicate headers happen in real sheets (they are renamed later); a row that is
    # mostly the same value is a title or a data row, not a header.
    return textual >= max(2, int(len(non_empty) * 0.6)) and len(set(non_empty)) >= max(2, int(len(non_empty) * 0.6))


def detect_header_row(rows: Sequence[Sequence[Any]], max_scan: int = 15) -> int | None:
    """Return the index of the header row, skipping titles and blank leading rows."""

    best: int | None = None
    best_score = 0.0
    for index, row in enumerate(rows[:max_scan]):
        if not _looks_like_header(row):
            continue
        cells = [_clean_header(cell) for cell in row]
        score = sum(1 for cell in cells if cell)
        # Header cells are short labels without long digit runs; data rows are not.
        score += sum(1 for cell in cells if cell and len(cell) <= 40 and not re.search(r"\d{4,}", cell) and not "@" in cell) * 0.5
        # Prefer a header followed by a data row of similar width.
        if index + 1 < len(rows):
            following = [cell for cell in rows[index + 1] if _clean_header(cell)]
            if len(following) >= max(2, int(score) // 2):
                score += 2
        if score > best_score:
            best, best_score = index, score
    return best


def dedupe_headers(headers: Sequence[Any]) -> tuple[str, ...]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for position, raw in enumerate(headers, start=1):
        name = _clean_header(raw) or f"column_{position}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        result.append(name)
    return tuple(result)


def grid_to_table(
    rows: Sequence[Sequence[Any]],
    *,
    name: str,
    source_file: str,
    sheet: str | None = None,
    page: int | None = None,
    ocr: bool = False,
    ocr_confidence: float | None = None,
    locator_prefix: str | None = None,
) -> ParsedTable:
    warnings: list[str] = []
    header_index = detect_header_row(rows)
    if header_index is None:
        return ParsedTable(name=name, headers=(), records=[], warnings=["no_header_row_detected"], page=page, ocr=ocr)
    if header_index > 0:
        warnings.append(f"skipped_{header_index}_leading_rows")
    headers = dedupe_headers(rows[header_index][:MAX_COLUMNS])
    records: list[IntermediateRecord] = []
    prefix = locator_prefix or (f"sheet={sheet}" if sheet else (f"page={page}" if page else "table"))
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        values = list(row[:len(headers)])
        if all(_clean_header(cell) == "" for cell in values):
            continue
        fields: dict[str, Any] = {}
        for header, value in zip(headers, values):
            fields[header] = value if not isinstance(value, str) else value.strip()
        records.append(IntermediateRecord(
            source_file=source_file,
            locator=f"{prefix};row={offset}",
            row_number=offset,
            fields=fields,
            sheet=sheet,
            page=page,
            ocr=ocr,
            ocr_confidence=ocr_confidence,
        ))
        if len(records) >= MAX_ROWS:
            warnings.append("row_limit_reached")
            break
    return ParsedTable(name=name, headers=headers, records=records, warnings=warnings, page=page, ocr=ocr)


__all__ = ["MAX_COLUMNS", "MAX_ROWS", "dedupe_headers", "detect_header_row", "grid_to_table"]
