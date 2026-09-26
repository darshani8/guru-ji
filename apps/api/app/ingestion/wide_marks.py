"""Wide marks sheets -> one exam row per student, course and exam.

Result sheets list one row per student and one column per subject, often under
EXTERNAL/INTERNAL (SEE/CIE) group labels: "EXTERNAL MARKS HRM (MBA201)", or a
plain "HRM (MBA201)" or "MBA201". The exam entity needs one row per student,
course and exam, so such a table is reshaped before staging. Only a table that
is clearly a marks sheet is touched: a student identifier column and at least
three columns whose headers carry a course code over mostly numeric values.
A reshaped table always goes to the mapping review, with the summary of the
reshape, before anything is imported, and a row that would change the marks
already stored for its student, course and exam waits for a decision too.
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

MIN_COURSES = 2
# The highest number a marks cell plausibly holds; a fee or a count above it is not a mark.
MAX_MARK = 200

# The column that names the student, best first: a USN, register number or student id, then
# a class roll number, then an admission or enrolment number (the student records are keyed by the first).
_ID_HEADERS = (
    re.compile(r"\b(?:usn|regno)\b|\b(?:student|reg|register|registration)\s(?:id|no|number)\b|^(?:student id|id|sid)$"),
    re.compile(r"\broll\s(?:id|no|number)\b|^roll$"),
    re.compile(r"\bprn\b|\b(?:enrol+ment|admission)\s(?:id|no|number)\b"),
)
_NAME_HEADER = re.compile(r"^(?:students?(?:\ss)?\s)?name(?:\sof(?:\sthe)?\sstudent)?$")
# Per-subject attendance looks like a marks sheet (a number per student and subject): a word
# of attendance (Attended, Att., Attd...) in the sheet name, the title or the header of any
# column of data rules the sheet out.
_ATTENDANCE = re.compile(r"\battend|\batt(?:d|n|en)?\b|\bpresent\b|\babsent\b|\bclasses\b|\bconducted\b|\bheld\b")
# So do grade points and credits per subject (8, 9, 10 or 4, 3): named in the sheet name, the
# title or a subject's header, or an SGPA/CGPA beside subjects that all hold 10 or less. The
# scheme a result title names ("Choice Based Credit System") is not a sheet of credits.
_GRADE_POINTS = re.compile(r"\bcredits?\b(?!\s(?:based|system|scheme))|\bgrade\s?points?\b|\bgp\b|\bgpa\b")
_GPA = re.compile(r"\b[sc]?gpa\b")
MAX_GRADE_POINT = 10
# Only in a subject's own header: result sheets have a Percentage column of their own.
_PERCENT = re.compile(r"\bpercent|%")
# Without a group label (EXTERNAL, CIE...) on the subjects, the sheet has to say it holds marks.
_MARKS_WORD = re.compile(r"\b(?:marks?|results?|scores?|grades?|sgpa|cgpa)\b")
# Sheet names that say nothing about which exam the sheet holds.
_GENERIC_SHEET = re.compile(r"(?:(?:sheet|table|data|page|worksheet|tab)\s?\d*\s?)+")
# A word naming an exam, alone or numbered ("IA2", "Test-1"), and the words that belong to
# the exam's name around it: an ordinal or number, or a kind of exam ("FIRST", "II", "MODEL").
_EXAM_WORD = re.compile(r"(?:ia|cie|see|tests?|exams?|examinations?|assessments?|internals?|mid|midterms?|terms?|quiz(?:zes)?|assignments?)\d{0,2}")
_EXAM_QUALIFIER = re.compile(
    r"\d{1,2}|[1-9](?:st|nd|rd|th)|first|second|third|fourth|fifth|i{1,3}|iv|vi{0,3}"
    r"|final|unit|end|class|model|prelims?|preliminary|periodic|supplementary|annual|semester|sem"
)
# An academic or financial year (AY2025, FY2024) or a semester, batch or year label is not a course code.
_NOT_A_COURSE = re.compile(r"[A-Z]{1,2}(?:19|20)\d\d|(?:SEM|BATCH|YEAR).*")
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
# A grace-marked cell: "45+2" is the 47 awarded, "40*" the 40 awarded.
_GRACE_MARK = re.compile(r"(?P<mark>\d+(?:\.\d+)?)\s*(?:\+\s*(?P<grace>\d+(?:\.\d+)?))?\s*\*?")


def _course_code(token: str) -> str | None:
    """A course code such as MBA201 or 21MBA14: letters and digits, at least two of each."""

    letters = sum(1 for char in token if char.isalpha())
    digits = sum(1 for char in token if char.isdigit())
    if letters < 2 or digits < 2 or _NOT_A_COURSE.fullmatch(token.upper()):
        return None
    return token.upper()


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


def _number(value: Any, *, grace: bool = True) -> int | float | None:
    """The number in a marks cell; with ``grace``, the total of a grace-marked cell ("45+2" -> 47)."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value == value else None  # NaN is not a mark
    text = str(value).strip()
    if _NUMBER.fullmatch(text):
        number = float(text)
    elif grace and (match := _GRACE_MARK.fullmatch(text)):
        number = float(match.group("mark")) + float(match.group("grace") or 0)
    else:
        return None
    return int(number) if number.is_integer() else number


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _exam_phrase(text: str) -> str | None:
    """The words naming an exam in a sheet name, title or file name ("FIRST INTERNAL ASSESSMENT", "IA-2"), if any."""

    words = [word.strip("()[]{}.,:;-") for word in re.split(r"[\s_]+", text)]
    keys = [re.sub(r"[^a-z0-9]", "", word.lower()) for word in words]
    words, keys = [word for word, key in zip(words, keys) if key], [key for key in keys if key]
    first = next((index for index, key in enumerate(keys) if _EXAM_WORD.fullmatch(key)), None)
    if first is None:
        return None
    start = end = first
    while start and (_EXAM_WORD.fullmatch(keys[start - 1]) or _EXAM_QUALIFIER.fullmatch(keys[start - 1])):
        start -= 1
    while end + 1 < len(keys) and (_EXAM_WORD.fullmatch(keys[end + 1]) or _EXAM_QUALIFIER.fullmatch(keys[end + 1])):
        end += 1
    return " ".join(words[start:end + 1])[:60]


def _exam_name(group: str, sheet: str = "", title: str = "", file_name: str = "") -> str:
    """The exam a column holds: its group label, else the exam the sheet name, the title
    or the file name names ("IA 2", "FIRST INTERNAL ASSESSMENT"), else the sheet's own
    name, else "Marks".

    This keeps two uploads of the same subjects (IA 1, then IA 2) apart: under one name
    the second would replace the first exam's marks (which the import then asks about).
    """

    normalized = normalize_header(group)
    if _EXTERNAL.fullmatch(normalized):
        return "SEE (External)"
    if _INTERNAL.fullmatch(normalized):
        return "CIE (Internal)"
    if group:
        return group
    stem = re.split(r"[\\/]", file_name)[-1].rsplit(".", 1)[0]
    phrase = next((found for found in map(_exam_phrase, (sheet, title, stem)) if found), None)
    if phrase:
        return _exam_name(phrase)
    sheet = " ".join(sheet.split())[:60]
    return sheet if normalize_header(sheet) and not _GENERIC_SHEET.fullmatch(normalize_header(sheet)) else "Marks"


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
    """The exam rows of a wide marks sheet and a summary of the reshape, or None for any other table.

    Anything that may not be a marks sheet is left alone (and so goes through the
    normal mapping review): a word of attendance (Attended, Att., Classes Held...),
    grade points or credits per subject, fewer than two
    different courses, numbers too large for marks, a subject whose sub-columns
    (CIE, SEE, Total) lost their headers, or no group label (EXTERNAL, CIE...)
    on the subjects and no word of marks (Marks, Result, Grade...) in the sheet
    name, its title or a header.
    """

    id_column = next((column for column in (_find_column(table.headers, pattern) for pattern in _ID_HEADERS) if column), None)
    if id_column is None or not table.records:
        return None
    # Values are judged on the students' rows, not on notes or footers under the table.
    students = [record for record in table.records if not _blank(record.fields.get(id_column))]
    context = [normalize_header(text) for text in (table.name, table.title, *table.headers)]
    # A legend beside the table ("AB means absent") heads a column without data and does not count.
    columns = [normalize_header(header) for header in table.headers if any(not _blank(record.fields.get(header)) for record in students)]
    if any(_ATTENDANCE.search(text) for text in [*context[:2], *columns]):
        return None
    name_column = _find_column([header for header in table.headers if header != id_column], _NAME_HEADER)
    labels: dict[str, str] = {}
    codes: dict[str, str] = {}
    marks: list[int | float] = []
    for position, header in enumerate(table.headers):
        if header in (id_column, name_column):
            continue
        parsed = _parse_course_header(header)
        if parsed is None:
            continue
        following = table.headers[position + 1] if position + 1 < len(table.headers) else ""
        if _UNNAMED.fullmatch(following) and any(_number(record.fields.get(following)) is not None for record in students):
            # A subject header spanning columns of numbers without headers of their own
            # (CIE, SEE and Total under it, lost): which column is which exam is unknown.
            return None
        values = [record.fields.get(header) for record in students]
        filled = [value for value in values if not _blank(value)]
        numbers = [number for number in map(_number, filled) if number is not None]
        if numbers and len(numbers) >= MIN_NUMERIC_SHARE * len(filled):
            labels[header], codes[header] = parsed
            marks.extend(numbers)
    if len(labels) < MIN_COURSE_COLUMNS or len(set(codes.values())) < MIN_COURSES:
        return None
    if any(_PERCENT.search(header.lower()) for header in labels) or not all(0 <= mark <= MAX_MARK for mark in marks):
        return None
    if any(_GRADE_POINTS.search(text) for text in [*context[:2], *map(normalize_header, labels)]):
        return None
    if all(mark <= MAX_GRADE_POINT for mark in marks) and any(_GPA.search(text) for text in context):
        return None
    split = _split_labels(labels, codes)
    grouped = any(split[header][0] and _GROUP.fullmatch(split[header][0]) for header in labels)
    if not grouped and not any(_MARKS_WORD.search(text) for text in context):
        return None

    exams: dict[str, str] = {}
    taken: set[tuple[str, str]] = set()
    for header in table.headers:
        if header not in labels:
            continue
        exam, number = _exam_name(split[header][0], table.name, table.title, table.records[0].source_file), 1
        # Two columns for the same course and exam (a repeated header) stay two exams, never one overwriting the other.
        while (codes[header], exam if number == 1 else f"{exam} ({number})") in taken:
            number += 1
        exams[header] = exam if number == 1 else f"{exam} ({number})"
        taken.add((codes[header], exams[header]))
    positions = {header: index for index, header in enumerate(table.headers)}

    records: list[IntermediateRecord] = []
    skipped: list[dict[str, Any]] = []
    without_marks: list[int] = []
    blank_cells = student_rows = grace_cells = 0
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
            # A cell such as AB (absent) or F is the result, not a mark; a grace-marked
            # one ("45+2", "40*") is both: the marks it adds up to and its own text.
            status = None if _number(value, grace=False) is not None else str(value).strip()
            if mark is not None and status is not None:
                grace_cells += 1
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
                    "result_status": status,
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
        "grace_mark_cells": grace_cells,
        # At a glance, for the mapping review: which exams and courses the rows hold.
        "exam_names": list(dict.fromkeys(exams[header] for header in labels)),
        "courses": list(dict.fromkeys(codes[header] for header in labels)),
    }
    warnings.append(f"reshaped_{table.row_count}_wide_rows_to_{len(records)}_exam_rows")
    reshaped = ParsedTable(name=table.name, headers=EXAM_HEADERS, records=records, warnings=warnings, page=table.page, ocr=table.ocr, title=table.title)
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
