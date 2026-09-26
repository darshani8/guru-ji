"""Shared header detection for grids coming from CSV, spreadsheets, and tables."""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from typing import Any

from ..models import IntermediateRecord, ParsedTable

MAX_ROWS = 100_000
MAX_COLUMNS = 200


_NUMBER = re.compile(r"[-+]?\d+(\.\d+)?%?")
_DATE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}([T ]\d{1,2}:\d{2}(:\d{2})?)?")
_WHOLE = re.compile(r"\d{1,4}")
# A header that names an identifier column: the USN, name, number, roll, register or id of a person.
_IDENTIFIER_LABEL = re.compile(r"\b(usn|seat|name|no|number|roll|reg|regd|register|registration|id|enrol+ment|admission|adm|prn|ticket|student|candidate)\b", re.IGNORECASE)
# What a mark cell holds for a student with no mark (absent, not eligible, malpractice...).
_ABSENCE_CODES = frozenset({"A", "AB", "ABS", "ABSENT", "-", "--", "NA", "N/A", "NE", "MP", "X"})


def _clean_header(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        # xlrd reads every number as a float, so a 2023 header cell arrives as 2023.0.
        value = int(value)
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


def _best_scoring_row(rows: Sequence[Sequence[Any]], max_scan: int) -> int | None:
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


def _kind(cell: str) -> str:
    """What a cleaned cell holds: blank, number, date, email, code (an id such as 1MS23MBA001) or text."""

    if not cell:
        return ""
    if _NUMBER.fullmatch(cell):
        return "number"
    if _DATE.fullmatch(cell):
        return "date"
    if "@" in cell:
        return "email"
    if " " not in cell and any(char.isdigit() for char in cell) and any(char.isalpha() for char in cell):
        return "code"
    return "text"


def _numbered_columns(cells: Sequence[str]) -> set[int]:
    """Positions of column numbers: the days 1..31 of an attendance register, or years.

    They are whole numbers in neighbouring cells counting by one from 1 (or up or
    down from a year), which a header row does and a data row almost never does.
    """

    positions = [index for index, cell in enumerate(cells) if _WHOLE.fullmatch(cell)]
    if len(positions) < 2:
        return set()
    numbers = [int(cells[index]) for index in positions]
    step = numbers[1] - numbers[0]
    if step not in (1, -1) or (numbers[0] != 1 and not 1900 <= numbers[0] <= 2100) or (step == -1 and numbers[0] < 1900):
        return set()
    for left, right, number, following in zip(positions, positions[1:], numbers, numbers[1:]):
        if right != left + 1 or following != number + step:
            return set()
    return set(positions)


def _plausible_header(cells: Sequence[str], numbered: set[int]) -> bool:
    """Labels only: text, ids, dates or column numbers, with at least one word, no values."""

    non_empty = [cell for cell in cells if cell]
    if len(non_empty) < 2 or len(set(non_empty)) < max(2, int(len(non_empty) * 0.6)):
        return False
    has_text = False
    for index, cell in enumerate(cells):
        if not cell or index in numbered:
            continue
        kind = _kind(cell)
        if kind in ("number", "email") or re.search(r"\d{5,}", cell) or len(cell) > 60:
            return False
        has_text = has_text or kind == "text"
    return has_text


@functools.lru_cache(maxsize=1)
def _known_column_names() -> frozenset[str]:
    # Imported lazily: the normalization package imports the ingestion models.
    from ...normalization.canonical import CANONICAL_ENTITIES

    names: set[str] = set()
    for entity in CANONICAL_ENTITIES.values():
        for field in entity.fields:
            names.add(field.name.replace("_", " "))
            names.update(field.synonyms)
    return frozenset(names)


def _known(cells: Sequence[str]) -> int:
    known = _known_column_names()
    return sum(1 for cell in cells if cell and " ".join(cell.lower().rstrip(".").split()) in known)


def _data_votes(cells: Sequence[str], below: Sequence[str], numbered: set[int]) -> int:
    """Cells that sit over the same kind of id, number, date or email, or the same value."""

    votes = 0
    for index, cell in enumerate(cells):
        under = below[index] if index < len(below) else ""
        if not cell or not under or index in numbered:
            continue
        kind = _kind(cell)
        if cell == under or (kind != "text" and kind == _kind(under)):
            votes += 1
    return votes


def _header_score(cells: Sequence[str], below: Sequence[str]) -> tuple[int, int]:
    """(known column names, how header-like the row is over the row below it)."""

    numbered = _numbered_columns(cells)
    named = 0
    for index, cell in enumerate(cells):
        under = below[index] if index < len(below) else ""
        if cell and (index in numbered or (under and _kind(cell) != _kind(under))):
            named += 1
    known = _known(cells)
    return known, known + named - _data_votes(cells, below, numbered)


def _header_above(grid: Sequence[Sequence[str]], chosen: int) -> int | None:
    """A header right above the chosen row when the chosen row is really the first data row.

    That happens when a header cell is blank (the data row is wider) or holds a
    number such as a year (it misses the short-label bonus). Rows are only ever
    moved up, so no row the width score kept as data is lost.
    """

    row = grid[chosen]
    below = next((other for other in grid[chosen + 1:chosen + 6] if any(other)), [])
    width = [index for index, cell in enumerate(row) if cell]
    index = chosen - 1
    lower = row
    for _ in range(5):
        while index >= 0 and not any(grid[index]):
            index -= 1
        if index < 0:
            return None
        cells = grid[index]
        # A copy of the header further down is not a data row.
        under = next((other for other in grid[chosen + 1:chosen + 6] if any(other) and other != cells), below)
        chosen_known, chosen_score = _header_score(row, under)
        numbered = _numbered_columns(cells)
        if _plausible_header(cells, numbered) and not _data_votes(cells, lower, numbered):
            data_columns = {position for position, cell in enumerate(row) if cell} | {position for position, cell in enumerate(under) if cell}
            sticks_out = any(cell and position not in data_columns for position, cell in enumerate(cells))
            unlabelled = sum(1 for position in width if position >= len(cells) or not cells[position])
            known, score = _header_score(cells, lower)
            # The chosen row must look like data (the same kinds of values or the same
            # values as the row under it), or this row must name more known columns.
            looks_like_data = _data_votes(row, under, _numbered_columns(row)) > 0 or (known >= 2 and known > chosen_known)
            if not sticks_out and unlabelled <= max(1, len(width) // 5) and score >= 2 and score > chosen_score and known >= chosen_known and looks_like_data:
                return index
            return None
        # Another data row of the same table (the same kinds of ids and numbers): keep climbing.
        if not _data_votes(cells, row, set()) or _known(cells):
            return None
        lower = cells
        index -= 1
    return None


def detect_header_row(rows: Sequence[Sequence[Any]], max_scan: int = 15) -> int | None:
    """Return the index of the header row, skipping titles and blank leading rows."""

    best = _best_scoring_row(rows, max_scan)
    grid = [[_clean_header(cell) for cell in row] for row in rows[:max_scan + 5]]
    if best is None:
        # Column numbers (days 1..31, years) are labels when the rest of the row is.
        for index, cells in enumerate(grid[:max_scan]):
            numbered = _numbered_columns(cells)
            if numbered and _plausible_header(cells, numbered):
                return index
        return None
    above = _header_above(grid, best)
    return best if above is None else above


def _cell(cells: Sequence[str], column: int) -> str:
    return cells[column] if column < len(cells) else ""


def _column_kinds(rows: Sequence[Sequence[Any]], start: int) -> tuple[dict[int, str], dict[int, set[str]]]:
    """The usual kind of value in each column, and the values, over the first few non-blank rows from ``start``."""

    kinds: dict[int, dict[str, int]] = {}
    values: dict[int, set[str]] = {}
    seen = 0
    for row in rows[start:start + 50]:
        cells = [_clean_header(cell) for cell in row[:MAX_COLUMNS]]
        if not any(cells):
            continue
        for column, cell in enumerate(cells):
            if cell:
                counts = kinds.setdefault(column, {})
                counts[_kind(cell)] = counts.get(_kind(cell), 0) + 1
                values.setdefault(column, set()).add(cell)
        seen += 1
        if seen == 6:
            break
    return {column: max(counts, key=counts.__getitem__) for column, counts in kinds.items()}, values


def _labels_over_data(cells: Sequence[str], kinds: dict[int, str], values: dict[int, set[str]]) -> tuple[int, int]:
    """(labels over a different kind of data, cells that look like the data under them)."""

    numbered = _numbered_columns(cells)
    named = data = 0
    for column, cell in enumerate(cells):
        kind = kinds.get(column)
        if not cell or not kind:
            continue
        if cell in values[column] or (column not in numbered and _kind(cell) != "text" and _kind(cell) == kind):
            data += 1
        elif column in numbered or _kind(cell) != kind:
            named += 1
    return named, data


def _combine_header_rows(upper: Sequence[str], lower: Sequence[str], width: int, merged: dict[int, int]) -> tuple[list[str], list[tuple[str, ...]]] | None:
    """One header per column from a group label row over a sub-label row, and the sub-labels under each group label.

    A group label covers its merged range when the reader knows the merges.
    Otherwise a label over a sub-label of its own spans rightwards over blank
    upper cells while the lower row has labels, up to the next upper label.
    None when a sub-label sits under a one-column label: then the lower row
    is data or a separate line, not part of the header.
    """

    owner: dict[int, int] = {}
    groups: dict[int, tuple[str, ...]] = {}
    for column in range(width):
        if not _cell(upper, column) or column in owner:
            continue
        end = merged.get(column)
        if end is None:
            end = column
            while _cell(lower, column) and end + 1 < width and not _cell(upper, end + 1) and _cell(lower, end + 1):
                end += 1
        end = max(column, min(end, width - 1))
        if end > column:
            groups[column] = tuple(_cell(lower, covered) for covered in range(column, end + 1) if _cell(lower, covered))
        for covered in range(column, end + 1):
            owner[covered] = column
    headers: list[str] = []
    for column in range(width):
        first = owner.get(column)
        group = _cell(upper, first) if first is not None else ""
        label = _cell(lower, column)
        if label and group and first not in groups:
            return None
        combined = f"{group} {label}" if group and label else group or label
        # Sub-labels under one group name different columns; repeats (AB, AB) are values.
        if label and combined in headers:
            return None
        headers.append(combined)
    return headers, list(groups.values())


def _header_block(rows: Sequence[Sequence[Any]], header_index: int, merged: Sequence[tuple[int, int, int, int]]) -> tuple[int, int, list[Any]]:
    """(first header row, last header row, one header per column).

    Two header rows are read as one when the row under the detected header row
    (or the detected row under the row above it) labels the columns a group label
    spans, or columns the upper row leaves blank: "EXTERNAL MARKS" over six
    subjects gives "EXTERNAL MARKS Accounts (MB101)" and so on, while "USN" over
    a blank cell stays "USN". The lower row must hold labels over a different
    kind of data, never values like the data below.
    """

    def cleaned(index: int) -> list[str]:
        return [_clean_header(cell) for cell in rows[index][:MAX_COLUMNS]]

    def spans(index: int) -> dict[int, int]:
        return {first_col: last_col for first_row, first_col, last_row, last_col in merged if first_row == index and last_col > first_col}

    for upper_index in (header_index, header_index - 1):
        lower_index = upper_index + 1
        if upper_index < 0 or lower_index >= len(rows):
            continue
        upper, lower = cleaned(upper_index), cleaned(lower_index)
        kinds, values = _column_kinds(rows, lower_index + 1)
        # Notes beside the table (a legend) do not decide whether the rows are headers.
        in_table = [[cell if column in kinds or _cell(other, column) else "" for column, cell in enumerate(cells)] for cells, other in ((upper, lower), (lower, upper))]
        if not all(_plausible_header(cells, _numbered_columns(cells)) for cells in in_table):
            continue
        width = min(max(len(upper), len(lower)), MAX_COLUMNS)
        combined = _combine_header_rows(upper, lower, width, spans(upper_index))
        if combined is None:
            continue
        headers, groups = combined
        if upper_index == header_index:
            # The row under the detected header: sub-labels over data, never data itself.
            named, data = _labels_over_data(lower, kinds, values)
            if named >= 2 and not data:
                return upper_index, lower_index, headers
        else:
            # The row above the detected header: group labels, and labels of its own
            # for columns the detected row leaves blank (the USN and name over a
            # block of subjects), never a title or key/value line over a header,
            # even one with a blank cell or two. The labels of its own lead the row
            # ("USN | Marks..."), one of them names an identifier, while a title or
            # key/value line starts over a labelled header cell. Or the same
            # sub-labels sit under two group labels (the subjects under IA1 and IA2).
            gaps = [column for column, cell in enumerate(upper) if cell and not _cell(lower, column) and column in kinds]
            first = next((column for column, cell in enumerate(upper) if cell and (column in kinds or _cell(lower, column))), None)
            fills_gap = first in gaps and any(_IDENTIFIER_LABEL.search(upper[column]) for column in gaps)
            sub_labels = [labels for labels in groups if labels]
            regrouped = len(set(sub_labels)) < len(sub_labels)
            if groups and gaps and (fills_gap or regrouped) and not _labels_over_data(upper, kinds, values)[1]:
                return upper_index, lower_index, headers
    return header_index, header_index, list(rows[header_index][:MAX_COLUMNS])


def _annotation_row(cells: Sequence[str], kinds: dict[int, str], grouped: bool = False) -> list[int] | None:
    """The identifier columns an annotation row under the headers leaves blank, or None for a data row.

    Such a row (the initials of the faculty under each subject) has nothing in
    the columns of ids or names and only short labels over columns of numbers.
    A row with a name is data, and so is a row of absence codes (AB, A, -) in
    the mark cells under a one-row header: a student with no id, never labels.
    Under a ``grouped`` two-row header the initials may happen to read as codes (AB, MP).
    """

    identifiers = [column for column, kind in kinds.items() if kind in ("code", "text")]
    labels = [column for column, cell in enumerate(cells) if cell and column in kinds]
    if not identifiers or any(_cell(cells, column) for column in identifiers) or len(labels) < 2:
        return None
    if any(kinds[column] != "number" or _kind(cells[column]) != "text" or len(cells[column]) > 16 for column in labels):
        return None
    if not grouped and all(cells[column].upper().replace(".", "") in _ABSENCE_CODES for column in labels):
        return None
    return sorted(identifiers)


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
    merged: Sequence[tuple[int, int, int, int]] = (),
) -> ParsedTable:
    """Read a grid into a table; ``merged`` holds merged cell ranges (first row, first column, last row, last column; 0-based)."""

    warnings: list[str] = []
    header_index = detect_header_row(rows)
    if header_index is None:
        return ParsedTable(name=name, headers=(), records=[], warnings=["no_header_row_detected"], page=page, ocr=ocr)
    first_header, last_header, header_cells = _header_block(rows, header_index, merged)
    if first_header > 0:
        warnings.append(f"skipped_{first_header}_leading_rows")
    if last_header > first_header:
        warnings.append(f"headers_combined_from_rows_{first_header + 1}_and_{last_header + 1}")
    # Keep every column that holds data, even when its header cell is blank or
    # missing (a spreadsheet row ends at its last filled cell).
    width = len(header_cells)
    for row in rows[last_header + 1:last_header + 1 + MAX_ROWS]:
        if len(row) > width:
            width = max(width, max((position + 1 for position, cell in enumerate(row[:MAX_COLUMNS]) if _clean_header(cell)), default=0))
    headers = dedupe_headers(header_cells + [""] * (min(width, MAX_COLUMNS) - len(header_cells)))
    # A row of notes right under the headers (the faculty teaching each subject) is not data.
    annotation: int | None = None
    if last_header + 1 < len(rows):
        blank = _annotation_row([_clean_header(cell) for cell in rows[last_header + 1][:len(headers)]], _column_kinds(rows, last_header + 2)[0], last_header > first_header)
        if blank:
            annotation = last_header + 2
            columns = ", ".join(headers[column] for column in blank if column < len(headers))
            warnings.append(f"skipped_row_{annotation}_as_annotation:blank {columns}; only short labels over number columns")
    records: list[IntermediateRecord] = []
    prefix = locator_prefix or (f"sheet={sheet}" if sheet else (f"page={page}" if page else "table"))
    for offset, row in enumerate(rows[last_header + 1:], start=last_header + 2):
        values = list(row[:len(headers)])
        if offset == annotation or all(_clean_header(cell) == "" for cell in values):
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
    title = " ".join(cell for row in rows[:first_header] for cell in map(_clean_header, row[:MAX_COLUMNS]) if cell)[:200]
    return ParsedTable(name=name, headers=headers, records=records, warnings=warnings, page=page, ocr=ocr, title=title)


__all__ = ["MAX_COLUMNS", "MAX_ROWS", "dedupe_headers", "detect_header_row", "grid_to_table"]
