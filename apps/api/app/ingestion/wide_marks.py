"""Wide marks sheets -> one exam row per student, course and exam.

Result sheets list one row per student and one column per subject, often under
EXTERNAL/INTERNAL (SEE/CIE) group labels: "EXTERNAL MARKS HRM (MBA201)", or a
plain "HRM (MBA201)" or "MBA201". The exam entity needs one row per student,
course and exam, so such a table is reshaped before staging. Only a table that
is clearly a marks sheet is touched: a student identifier column and at least
three columns whose headers carry a course code over mostly numeric values.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from ..normalization.mapping import normalize_header
from .models import IntermediateRecord, ParseResult, ParsedTable
from .parsers.tabular import MAX_ROWS

EXAM_HEADERS = ("student_id", "student_name", "course_code", "course_name", "exam_name", "marks_obtained", "result_status")
MIN_COURSE_COLUMNS = 3
# Share of a course column's filled cells that must be numbers ("mostly numeric").
MIN_NUMERIC_SHARE = 0.6
# How many skipped rows the report names one by one.
REPORT_ROWS = 50

_ID_HEADER = re.compile(r"\b(?:usn|prn|regno)\b|\b(?:student|roll|reg|register|registration|enrol+ment|admission)\s(?:id|no|number)\b|^(?:student id|id|roll|sid)$")
_NAME_HEADER = re.compile(r"^(?:students?(?:\ss)?\s)?name(?:\sof(?:\sthe)?\sstudent)?$")
# A marks sheet says so somewhere (its name, a group label, a Total or Result column);
# per-subject attendance looks the same otherwise and is never reshaped.
_MARKS_SIGNAL = re.compile(r"\b(?:marks?|external|internal|see|cie|ia\d*|exams?|examination|tests?|results?|total|grade|sgpa|cgpa|scores?|sem|semester)\b")
_NOT_MARKS = re.compile(r"\battend|\bpercent|%|\bpresent\b|\babsent\b|\bclasses\b|\bconducted\b")
# The suffix dedupe_headers gives a repeated header ("HRM (MBA201) (2)"), and its name for a blank one.
_UNNAMED = re.compile(r"column_\d+")
_REPEAT_SUFFIX = re.compile(r"\s\(\d+\)$")
_BRACKETED_CODE = re.compile(r"^(?P<label>.*?)\s*[(\[]\s*(?P<code>[A-Za-z0-9]{4,12})\s*[)\]]$")
_BARE_CODE = re.compile(r"^(?:(?P<label>.*)\s)?(?P<code>[A-Z0-9]{4,12})$")
# A group label in front of the subject: EXTERNAL MARKS, SEE, CIE 1, Internal Assessment...
_GROUP = re.compile(
    r"^(?P<group>(?:external|internal|see|cie|ia|semester end|continuous internal)"
    r"(?:\s(?:marks?|exam|examination|assessment|evaluation|test))?(?:\s?-?\s?\d{1,2})?)(?:\s(?P<subject>.+))?$",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(r"^(?:external|see|semester end)(?: marks?| exam| examination| evaluation)?$")
_INTERNAL = re.compile(r"^(?:internal|cie|ia|continuous internal)(?: marks?| assessment| evaluation| exam)?$")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _course_code(token: str) -> str | None:
    """A course code such as MBA201 or 21MBA14: letters and digits, at least two of each."""

    letters = sum(1 for char in token if char.isalpha())
    digits = sum(1 for char in token if char.isdigit())
    return token.upper() if letters >= 2 and digits >= 2 else None


def _parse_course_header(header: str) -> tuple[str, str] | None:
    """(label before the code, course code) for a subject column, else None."""

    text = _REPEAT_SUFFIX.sub("", header).strip()
    match = _BRACKETED_CODE.match(text) or _BARE_CODE.match(text)
    if not match:
        return None
    code = _course_code(match.group("code"))
    return ((match.group("label") or "").strip(), code) if code else None


def _blank(value: Any) -> bool:
    # A dash or dot in a marks cell means no mark was entered.
    return value is None or not re.sub(r"[\s.\-\u2013\u2014]", "", str(value))


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value == value else None  # NaN is not a mark
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        return None
    number = float(text)
    return int(number) if number.is_integer() else number


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _exam_name(group: str) -> str:
    normalized = normalize_header(group)
    if _EXTERNAL.fullmatch(normalized):
        return "SEE (External)"
    if _INTERNAL.fullmatch(normalized):
        return "CIE (Internal)"
    return group or "Marks"


def _common_suffix(labels: Sequence[str]) -> list[str]:
    words = [label.split() for label in labels]
    suffix: list[str] = []
    while all(len(item) > len(suffix) for item in words):
        candidates = {item[-1 - len(suffix)].lower() for item in words}
        if len(candidates) != 1:
            break
        suffix.insert(0, words[0][-1 - len(suffix)])
    return suffix


def _split_labels(labels: dict[str, str], codes: dict[str, str]) -> dict[str, tuple[str, str]]:
    """(group label, subject) for each course column's label.

    A known group label (EXTERNAL, INTERNAL MARKS, SEE, CIE...) counts only when
    at least two columns share it, so a lone subject such as "Internal Audit"
    keeps its name. Otherwise, when a course appears in several columns, the
    words those columns share at the end are the subject and the rest is the
    group ("MID TERM HRM" and "FINAL HRM").
    """

    split: dict[str, tuple[str, str]] = {}
    known = {header: _GROUP.match(label) for header, label in labels.items()}
    support = Counter(normalize_header(match.group("group")) for match in known.values() if match)
    for header, match in known.items():
        if match and support[normalize_header(match.group("group"))] >= 2:
            split[header] = (match.group("group"), (match.group("subject") or "").strip())
    by_code: dict[str, list[str]] = {}
    for header in labels:
        if header not in split:
            by_code.setdefault(codes[header], []).append(header)
    for headers in by_code.values():
        subject = _common_suffix([labels[header] for header in headers]) if len(headers) > 1 else []
        for header in headers:
            words = labels[header].split()
            group = " ".join(words[: len(words) - len(subject)]) if subject else ""
            split[header] = (group, " ".join(subject) if subject and group else labels[header])
    return split


def _find_column(headers: Sequence[str], pattern: re.Pattern[str]) -> str | None:
    return next((header for header in headers if pattern.search(normalize_header(_REPEAT_SUFFIX.sub("", header)))), None)


def reshape_marks_table(table: ParsedTable) -> tuple[ParsedTable, dict[str, Any]] | None:
    """The exam rows of a wide marks sheet and a summary of the reshape, or None for any other table."""

    id_column = _find_column(table.headers, _ID_HEADER)
    if id_column is None or not table.records:
        return None
    name_column = _find_column([header for header in table.headers if header != id_column], _NAME_HEADER)
    # Values are judged on the students' rows, not on notes or footers under the table.
    students = [record for record in table.records if not _blank(record.fields.get(id_column))]
    labels: dict[str, str] = {}
    codes: dict[str, str] = {}
    for header in table.headers:
        if header in (id_column, name_column):
            continue
        parsed = _parse_course_header(header)
        if parsed is None:
            continue
        values = [record.fields.get(header) for record in students]
        filled = [value for value in values if not _blank(value)]
        numeric = sum(1 for value in filled if _number(value) is not None)
        if numeric and numeric >= MIN_NUMERIC_SHARE * len(filled):
            labels[header], codes[header] = parsed
    if len(labels) < MIN_COURSE_COLUMNS:
        return None
    course_text = [normalize_header(table.name)] + [header.lower() for header in labels]
    if any(_NOT_MARKS.search(text) for text in course_text):
        return None
    if not any(_MARKS_SIGNAL.search(normalize_header(text)) for text in [table.name, *table.headers]):
        return None

    split = _split_labels(labels, codes)
    exams: dict[str, str] = {}
    taken: set[tuple[str, str]] = set()
    for header in table.headers:
        if header not in labels:
            continue
        exam, number = _exam_name(split[header][0]), 1
        # Two columns for the same course and exam (a repeated header) stay two exams, never one overwriting the other.
        while (codes[header], exam if number == 1 else f"{exam} ({number})") in taken:
            number += 1
        exams[header] = exam if number == 1 else f"{exam} ({number})"
        taken.add((codes[header], exams[header]))
    positions = {header: index for index, header in enumerate(table.headers)}

    records: list[IntermediateRecord] = []
    skipped: list[dict[str, Any]] = []
    without_marks: list[int] = []
    blank_cells = student_rows = 0
    warnings = list(table.warnings)
    for record in table.records:
        student_id = record.fields.get(id_column)
        cells = {header: record.fields.get(header) for header in labels}
        has_mark = any(_number(value) is not None for value in cells.values())
        if _blank(student_id) and not has_mark:
            # A note or footer line (or an annotation row): nothing to import, but say so.
            skipped.append({"row": record.row_number, "reason": "no student identifier and no marks"})
            continue
        # A row with marks but no identifier is kept: validation rejects it where the uploader can see it.
        emitted = 0
        for header, value in cells.items():
            if _blank(value):
                blank_cells += 1
                continue
            mark = _number(value)
            subject = split[header][1]
            records.append(IntermediateRecord(
                source_file=record.source_file,
                locator=f"{record.locator};col={_column_letter(positions[header])}",
                row_number=record.row_number,
                fields={
                    "student_id": None if _blank(student_id) else student_id,
                    "student_name": record.fields.get(name_column) if name_column else None,
                    "course_code": codes[header],
                    "course_name": subject or None,
                    "exam_name": exams[header],
                    "marks_obtained": mark,
                    # A cell such as AB (absent) or F is the result, not a mark.
                    "result_status": None if mark is not None else str(value).strip(),
                },
                sheet=record.sheet,
                page=record.page,
                ocr=record.ocr,
                ocr_confidence=record.ocr_confidence,
            ))
            emitted += 1
        if emitted:
            student_rows += 1
        else:
            without_marks.append(record.row_number)
        if len(records) >= MAX_ROWS:
            warnings.append("row_limit_reached")
            break
    # Every other column (Sl. No, Total, Percentage, Result...) stays out of the exam rows;
    # only a blank column without a header of its own goes unmentioned.
    left_out = [
        header for header in table.headers
        if header not in labels and header not in (id_column, name_column)
        and (not _UNNAMED.fullmatch(header) or any(not _blank(record.fields.get(header)) for record in table.records))
    ]
    summary = {
        "sheet": table.name,
        "wide_rows": table.row_count,
        "exam_rows": len(records),
        "student_rows": student_rows,
        "student_id_column": id_column,
        "student_name_column": name_column,
        "course_columns": {header: {"course_code": codes[header], "course_name": split[header][1] or None, "exam_name": exams[header]} for header in labels},
        "left_out_columns": left_out,
        "skipped_rows": len(skipped),
        "skipped_row_details": skipped[:REPORT_ROWS],
        "rows_without_marks": without_marks[:REPORT_ROWS],
        "blank_mark_cells": blank_cells,
    }
    warnings.append(f"reshaped_{table.row_count}_wide_rows_to_{len(records)}_exam_rows")
    reshaped = ParsedTable(name=table.name, headers=EXAM_HEADERS, records=records, warnings=warnings, page=table.page, ocr=table.ocr)
    return reshaped, summary


def reshape_marks_sheets(result: ParseResult, *, entity: str | None = None) -> ParseResult:
    """``result`` with every wide marks table reshaped into exam rows.

    Nothing is reshaped when the uploader named another entity. The summary of
    each reshape is kept under ``metadata["reshaped_marks"]`` so it reaches the
    job report with the rest of the parse.
    """

    if entity not in (None, "", "exam"):
        return result
    tables: list[ParsedTable] = []
    summaries: list[dict[str, Any]] = []
    for table in result.tables:
        reshaped = reshape_marks_table(table) if table.headers else None
        if reshaped is None:
            tables.append(table)
            continue
        tables.append(reshaped[0])
        summaries.append(reshaped[1])
    if not summaries:
        return result
    return replace(result, tables=tables, metadata={**result.metadata, "reshaped_marks": summaries})


def reshaped_entity(headers: Sequence[str]) -> str | None:
    """The entity of a staged table the reshape produced: exam."""

    return "exam" if set(EXAM_HEADERS) <= set(headers) else None


__all__ = ["EXAM_HEADERS", "reshape_marks_sheets", "reshape_marks_table", "reshaped_entity"]
