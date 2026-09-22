"""Dependency-free CSV, XLSX, and PDF writers for generated reports."""

from __future__ import annotations

import csv
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from io import BytesIO, StringIO
from typing import Any
from xml.sax.saxutils import escape

MAX_REPORT_ROWS = 50_000


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return str(value)


_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


_NUMERIC_LITERAL = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _csv_cell_text(value: Any) -> str:
    """CSV cells are text; a leading formula trigger is neutralised with a quote.

    Spreadsheet applications evaluate cells that start with ``=``, ``+``, ``-``,
    ``@``, tab or carriage return as formulas (DDE / HYPERLINK exfiltration).
    Genuine numbers cannot carry a formula, so they keep their sign.
    """

    text = _cell_text(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return text
    if _NUMERIC_LITERAL.fullmatch(text):
        return text  # "+919876543210" or "-1500.50" cannot be a formula; keep the value intact
    if text.startswith(_FORMULA_TRIGGERS):
        return "'" + text
    return text


def render_csv(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([_csv_cell_text(column) for column in columns])
    for row in rows[:MAX_REPORT_ROWS]:
        writer.writerow([_csv_cell_text(row.get(column)) for column in columns])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def render_xlsx(columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, sheet_name: str = "Report") -> bytes:
    safe_sheet = "".join(char for char in sheet_name if char not in '[]:*?/\\')[:31] or "Report"
    cells: list[str] = []

    def cell(ref: str, value: Any, header: bool = False) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)) and not header:
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = escape(_cell_text(value))
        style = ' s="1"' if header else ""
        return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'

    cells.append('<row r="1">' + "".join(cell(f"{_column_letter(i)}1", name, header=True) for i, name in enumerate(columns)) + "</row>")
    for row_index, row in enumerate(rows[:MAX_REPORT_ROWS], start=2):
        cells.append(f'<row r="{row_index}">' + "".join(cell(f"{_column_letter(i)}{row_index}", row.get(name)) for i, name in enumerate(columns)) + "</row>")
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(cells) + "</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(safe_sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _pdf_escape(text: str) -> str:
    cleaned = text.encode("cp1252", errors="replace").decode("cp1252")
    return cleaned.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def render_pdf(title: str, lines: Sequence[str], *, subtitle: str | None = None, line_width: int = 95, lines_per_page: int = 58) -> bytes:
    """A small text PDF (Helvetica, A4) built by hand; no external library required."""

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body: list[str] = []
    for line in lines:
        body.extend(_wrap(str(line), line_width))
    pages: list[list[str]] = []
    header = [title[:line_width], (subtitle or f"Generated {generated}")[:line_width], ""]
    chunk_size = lines_per_page - len(header)
    for start in range(0, max(1, len(body)), chunk_size):
        pages.append(header + body[start:start + chunk_size])
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    pages_id_placeholder = len(objects) + 1 + len(pages) * 2  # computed after page objects
    content_ids: list[int] = []
    for page_number, page_lines in enumerate(pages, start=1):
        stream_lines = ["BT", "/F2 13 Tf", "50 800 Td", "14 TL"]
        for index, line in enumerate(page_lines):
            if index == 1:
                stream_lines.append("/F1 9 Tf")
            elif index == 3:
                stream_lines.append("/F1 10 Tf")
            stream_lines.append(f"({_pdf_escape(line)}) Tj T*")
        stream_lines.append(f"/F1 8 Tf 0 -10 Td (Page {page_number} of {len(pages)}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("cp1252", errors="replace")
        content_ids.append(add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"))
    for content_id in content_ids:
        page_ids.append(add(
            b"<< /Type /Page /Parent " + str(pages_id_placeholder).encode() + b" 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 " + str(font_id).encode() + b" 0 R /F2 " + str(bold_id).encode() + b" 0 R >> >> "
            b"/Contents " + str(content_id).encode() + b" 0 R >>"
        ))
    pages_id = add(b"<< /Type /Pages /Kids [" + b" ".join(f"{pid} 0 R".encode() for pid in page_ids) + b"] /Count " + str(len(page_ids)).encode() + b" >>")
    assert pages_id == pages_id_placeholder
    catalog_id = add(b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>")
    output = BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return output.getvalue()


def render_table_pdf(title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, subtitle: str | None = None) -> bytes:
    widths = {column: min(28, max(len(column), *(len(_cell_text(row.get(column))) for row in rows[:500]))) if rows else len(column) for column in columns}
    header = "  ".join(column[: widths[column]].ljust(widths[column]) for column in columns)
    lines = [header, "-" * len(header)]
    for row in rows[:MAX_REPORT_ROWS]:
        lines.append("  ".join(_cell_text(row.get(column))[: widths[column]].ljust(widths[column]) for column in columns))
    lines.append("")
    lines.append(f"{len(rows)} row(s)")
    return render_pdf(title, lines, subtitle=subtitle, line_width=max(95, len(header)))


FORMAT_CONTENT_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def render_report(format_name: str, title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, subtitle: str | None = None) -> tuple[bytes, str]:
    fmt = format_name.lower().strip()
    if fmt == "csv":
        return render_csv(columns, rows), FORMAT_CONTENT_TYPES["csv"]
    if fmt in {"xlsx", "excel"}:
        return render_xlsx(columns, rows, sheet_name=title[:31]), FORMAT_CONTENT_TYPES["xlsx"]
    if fmt == "pdf":
        return render_table_pdf(title, columns, rows, subtitle=subtitle), FORMAT_CONTENT_TYPES["pdf"]
    raise ValueError("report format must be csv, xlsx, or pdf")


__all__ = ["FORMAT_CONTENT_TYPES", "MAX_REPORT_ROWS", "render_csv", "render_pdf", "render_report", "render_table_pdf", "render_xlsx"]
