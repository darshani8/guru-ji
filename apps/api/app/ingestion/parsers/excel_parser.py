"""Workbook parsing.

``openpyxl`` is used when it is installed. Otherwise a small reader built on
the standard library handles the common institutional workbook: shared and
inline strings, numbers, booleans, and ISO dates. Legacy ``.xls`` binaries
need the optional ``xlrd`` package.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

from ..models import FileKind, ParseResult, ParsedTable, ParserError, ParserUnavailable
from .archive import check_archive_limits, open_entry, read_entry
from .tabular import MAX_ROWS, grid_to_table

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")
_EXCEL_EPOCH = datetime(1899, 12, 30)
# Built-in number formats Excel treats as dates (ECMA-376 §18.8.30).
_DATE_FORMAT_IDS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref)
    if not match:
        return 0
    letters = match.group(1)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _excel_serial_to_date(value: float) -> Any:
    try:
        result = _EXCEL_EPOCH + timedelta(days=float(value))
    except (OverflowError, ValueError):
        return value
    if result.hour == 0 and result.minute == 0 and result.second == 0:
        return result.date().isoformat()
    return result.isoformat()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Stream the shared string table; it can be as large as the sheets themselves."""

    try:
        source = open_entry(archive, "xl/sharedStrings.xml")
    except KeyError:
        return []
    strings: list[str] = []
    item_tag = f"{{{_NS['m']}}}si"
    text_tag = f"{{{_NS['m']}}}t"
    try:
        with source:
            for _, element in ElementTree.iterparse(source, events=("end",)):
                if element.tag != item_tag:
                    continue
                strings.append("".join(node.text or "" for node in element.iter(text_tag)))
                element.clear()
    except (ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise ParserError("workbook shared strings could not be read") from exc
    return strings


def _read_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    """Parse one small part of the package; ``KeyError`` when it is absent."""

    try:
        return ElementTree.fromstring(read_entry(archive, name))
    except ElementTree.ParseError as exc:
        raise ParserError(f"workbook part {name} is not well-formed XML") from exc


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    try:
        root = _read_xml(archive, "xl/styles.xml")
    except KeyError:
        return set()
    custom_date_formats: set[int] = set()
    for fmt in root.iterfind("m:numFmts/m:numFmt", _NS):
        code = (fmt.get("formatCode") or "").lower()
        stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", code)
        if re.search(r"[dmy]", stripped) and not re.search(r"[#0]", stripped):
            try:
                custom_date_formats.add(int(fmt.get("numFmtId", "-1")))
            except ValueError:
                continue
    styles: set[int] = set()
    for index, xf in enumerate(root.iterfind("m:cellXfs/m:xf", _NS)):
        try:
            fmt_id = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        if fmt_id in _DATE_FORMAT_IDS or fmt_id in custom_date_formats:
            styles.add(index)
    return styles


def _sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook = _read_xml(archive, "xl/workbook.xml")
        rels = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    except KeyError as exc:
        raise ParserError("workbook is missing its sheet index (xl/workbook.xml)") from exc
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels.findall("rel:Relationship", _NS)}
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iterfind("m:sheets/m:sheet", _NS):
        rel_id = sheet.get(f"{{{_NS['r']}}}id")
        target = targets.get(rel_id, "")
        if not target:
            continue
        path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if sheet.get("state") == "hidden":
            continue
        sheets.append((sheet.get("name", f"Sheet{len(sheets) + 1}"), path))
    return sheets


def _cell_value(cell: ElementTree.Element, shared: list[str], date_styles: set[int]) -> Any:
    cell_type = cell.get("t", "n")
    style = cell.get("s")
    value_node = cell.find("m:v", _NS)
    if cell_type == "inlineStr":
        node = cell.find("m:is", _NS)
        return "".join(t.text or "" for t in node.iter(f"{{{_NS['m']}}}t")) if node is not None else ""
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "d":
        return raw
    try:
        number = float(raw)
    except ValueError:
        return raw
    if style is not None:
        try:
            if int(style) in date_styles:
                return _excel_serial_to_date(number)
        except ValueError:
            pass
    if number.is_integer():
        return int(number)
    return number


def _read_sheet(archive: zipfile.ZipFile, path: str, shared: list[str], date_styles: set[int]) -> list[list[Any]]:
    """Stream a worksheet row by row so the whole XML tree is never held in memory."""

    rows: list[list[Any]] = []
    row_tag = f"{{{_NS['m']}}}row"
    with open_entry(archive, path) as source:
        for _, row in ElementTree.iterparse(source, events=("end",)):
            if row.tag != row_tag:
                continue
            values: list[Any] = []
            for cell in row.findall("m:c", _NS):
                index = _column_index(cell.get("r", ""))
                while len(values) < index:
                    values.append("")
                values.append(_cell_value(cell, shared, date_styles))
            rows.append(values)
            row.clear()
            if len(rows) > MAX_ROWS + 20:
                break
    return rows


def _open_archive(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParserError("workbook is not a valid .xlsx archive") from exc
    try:
        check_archive_limits(archive)
    except ParserError:
        archive.close()
        raise
    return archive


def _parse_with_stdlib(file_name: str, content: bytes) -> ParseResult:
    with _open_archive(content) as archive:
        shared = _shared_strings(archive)
        date_styles = _date_styles(archive)
        result = ParseResult(file_name=file_name, file_kind=FileKind.XLSX, metadata={"engine": "stdlib"})
        for sheet_name, path in _sheet_paths(archive):
            try:
                rows = _read_sheet(archive, path, shared, date_styles)
            except (KeyError, ElementTree.ParseError, zipfile.BadZipFile):
                result.warnings.append(f"sheet_unreadable:{sheet_name}")
                continue
            table = grid_to_table(rows, name=sheet_name, source_file=file_name, sheet=sheet_name)
            result.tables.append(table)
        result.page_count = len(result.tables)
    return result


def _parse_with_openpyxl(file_name: str, content: bytes) -> ParseResult:
    import openpyxl  # type: ignore[import-not-found]

    workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    result = ParseResult(file_name=file_name, file_kind=FileKind.XLSX, metadata={"engine": "openpyxl"})
    for sheet in workbook.worksheets:
        if sheet.sheet_state != "visible":
            continue
        rows: list[list[Any]] = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([
                (value.isoformat() if isinstance(value, (datetime, date)) else value)
                for value in row
            ])
            if len(rows) > MAX_ROWS + 20:
                break
        result.tables.append(grid_to_table(rows, name=sheet.title, source_file=file_name, sheet=sheet.title))
    result.page_count = len(result.tables)
    return result


def parse_xlsx(file_name: str, content: bytes) -> ParseResult:
    # The inflated-size check runs before either engine touches an entry.
    _open_archive(content).close()
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return _parse_with_stdlib(file_name, content)
    try:
        return _parse_with_openpyxl(file_name, content)
    except Exception:  # noqa: BLE001 - fall back to the dependency-free reader
        return _parse_with_stdlib(file_name, content)


def parse_xls(file_name: str, content: bytes) -> ParseResult:
    try:
        import xlrd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ParserUnavailable("legacy .xls workbooks need the optional xlrd package; save the file as .xlsx or .csv") from exc
    book = xlrd.open_workbook(file_contents=content)
    result = ParseResult(file_name=file_name, file_kind=FileKind.XLS, metadata={"engine": "xlrd"})
    for sheet in book.sheets():
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(min(sheet.nrows, MAX_ROWS + 20))]
        result.tables.append(grid_to_table(rows, name=sheet.name, source_file=file_name, sheet=sheet.name))
    result.page_count = len(result.tables)
    return result


def tables_only(result: ParseResult) -> list[ParsedTable]:
    return [table for table in result.tables if table.headers]


__all__ = ["parse_xls", "parse_xlsx", "tables_only"]
