"""Dependency-free CSV, XLSX, PDF, Word (DOCX) and PowerPoint (PPTX) writers for generated reports."""

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


# Characters XML 1.0 cannot carry at all; one in a cell would make Excel or Word
# refuse the whole file, so they are dropped rather than escaped.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml(text: str, *, attribute: bool = False) -> str:
    return escape(_XML_ILLEGAL.sub("", text), {'"': "&quot;"} if attribute else {})


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
        text = _xml(_cell_text(value))
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
        f'<sheets><sheet name="{_xml(safe_sheet, attribute=True)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
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


# Word and PowerPoint files are for reading, not for every row: past these
# limits the document says how many rows it leaves out and points to Excel.
MAX_DOCX_ROWS = 5_000
MAX_PPTX_ROWS = 240
MAX_PPTX_COLUMNS = 10
PPTX_ROWS_PER_SLIDE = 12
_ACCENT = "E8750F"  # saffron
_INK = "1F1A17"
_MUTED = "6B645C"
_RULE = "E6DED3"
_HEADER_FILL = "FCE9D6"
_ACRONYMS = {"id": "ID", "usn": "USN", "hod": "HOD", "mba": "MBA", "mca": "MCA", "cgpa": "CGPA", "sgpa": "SGPA", "url": "URL"}


def _label(column: str) -> str:
    """A column name as a heading people read: "attendance_percent" -> "Attendance Percent"."""

    words = [word for word in re.split(r"[_\s]+", str(column).strip()) if word]
    return " ".join(_ACRONYMS.get(word.lower(), word[:1].upper() + word[1:]) for word in words) or str(column)


def _generated_line(row_count: int, shown: int, subtitle: str | None) -> str:
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    rows = f"{row_count:,} row{'s' if row_count != 1 else ''}"
    parts = [subtitle.strip()] if subtitle and subtitle.strip() else []
    parts += [rows, f"Generated {generated} by Agent Saffron"]
    line = " · ".join(parts)
    if shown < row_count:
        line += f". Showing the first {shown:,}; ask for an Excel file to get every row."
    return line


def _core_properties(title: str) -> str:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml(title)}</dc:title><dc:creator>Agent Saffron</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created></cp:coreProperties>'
    )


_CORE_TYPE = '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
_CORE_REL = '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'


def _zip(parts: Mapping[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def render_docx(title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, subtitle: str | None = None) -> bytes:
    """A Word document: the title, a line saying what it holds, and the rows as a table."""

    shown = list(rows[:MAX_DOCX_ROWS])
    landscape = len(columns) > 6
    page_w, page_h = (16838, 11906) if landscape else (11906, 16838)
    margin = 1134  # 2 cm
    usable = page_w - 2 * margin
    col_w = max(400, usable // max(1, len(columns)))
    size = 18 if len(columns) <= 6 else 16 if len(columns) <= 10 else 14  # half-points

    def run(text: str, *, bold: bool = False, color: str | None = None, half_points: int | None = None) -> str:
        props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + (f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/>' if half_points else "")
        return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{_xml(text)}</w:t></w:r>'

    def cell(text: str, *, header: bool) -> str:
        shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{_HEADER_FILL}"/>' if header else ""
        return f'<w:tc><w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/>{shade}</w:tcPr><w:p>{run(text, bold=header, half_points=size) if text else ""}</w:p></w:tc>'

    border = f'w:val="single" w:sz="4" w:space="0" w:color="{_RULE}"'
    table = [
        '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders><w:top {border}/><w:left {border}/><w:bottom {border}/><w:right {border}/><w:insideH {border}/><w:insideV {border}/></w:tblBorders>"
        '<w:tblLayout w:type="autofit"/><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="90" w:type="dxa"/><w:bottom w:w="40" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar>'
        '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>',
        "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in columns) + "</w:tblGrid>",
        '<w:tr><w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>' + "".join(cell(_label(column), header=True) for column in columns) + "</w:tr>",
    ]
    for row in shown:
        table.append("<w:tr><w:trPr><w:cantSplit/></w:trPr>" + "".join(cell(_cell_text(row.get(column)), header=False) for column in columns) + "</w:tr>")
    table.append("</w:tbl>")
    orient = ' w:orient="landscape"' if landscape else ""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        f'<w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="24" w:space="6" w:color="{_ACCENT}"/></w:pBdr><w:spacing w:after="80"/></w:pPr>{run(title, bold=True, color=_INK, half_points=36)}</w:p>'
        f'<w:p><w:pPr><w:spacing w:after="240"/></w:pPr>{run(_generated_line(len(rows), len(shown), subtitle), color=_MUTED, half_points=18)}</w:p>'
        + "".join(table)
        + f'<w:p/><w:sectPr><w:pgSz w:w="{page_w}" w:h="{page_h}"{orient}/><w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" w:left="{margin}" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults>'
        '<w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-IN"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults></w:styles>'
    )
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            f"{_CORE_TYPE}</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>{_CORE_REL}</Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
        ),
        "word/document.xml": document,
        "word/styles.xml": styles,
        "docProps/core.xml": _core_properties(title),
    })


_P_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_SLIDE_W, _SLIDE_H = 12192000, 6858000  # 16:9
_EMPTY_TREE = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'


def _pptx_text(text: str, *, size: int, color: str, bold: bool = False) -> str:
    weight = ' b="1"' if bold else ""
    return f'<a:r><a:rPr lang="en-IN" sz="{size}"{weight} dirty="0"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:rPr><a:t>{_xml(text)}</a:t></a:r>'


def _pptx_box(shape_id: int, name: str, x: int, y: int, cx: int, cy: int, paragraphs: Sequence[str], *, anchor: str = "t") -> str:
    body = "".join(f"<a:p>{paragraph}</a:p>" for paragraph in paragraphs)
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" lIns="0" rIns="0" anchor="{anchor}"><a:normAutofit/></a:bodyPr><a:lstStyle/>{body}</p:txBody></p:sp>'
    )


def _pptx_bar(shape_id: int, x: int, y: int, cx: int, cy: int) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Accent"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{_ACCENT}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    )


def _pptx_slide(shapes: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld {_P_NS}><p:cSld><p:spTree>{_EMPTY_TREE}{shapes}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'


def _pptx_table(columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, x: int, y: int, width: int) -> str:
    size = 1200 if len(columns) <= 6 else 1000 if len(columns) <= 8 else 900
    col_w = width // max(1, len(columns))
    row_h = 370840

    def line(side: str) -> str:
        return f'<a:{side} w="6350"><a:solidFill><a:srgbClr val="{_RULE}"/></a:solidFill></a:{side}>'

    borders = "".join(line(side) for side in ("lnL", "lnR", "lnT", "lnB"))

    def cell(text: str, *, header: bool) -> str:
        run = _pptx_text(text, size=size, color="FFFFFF" if header else _INK, bold=header) if text else f'<a:endParaRPr lang="en-IN" sz="{size}"/>'
        fill = f'<a:solidFill><a:srgbClr val="{_ACCENT}"/></a:solidFill>' if header else '<a:noFill/>'
        return f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{run}</a:p></a:txBody><a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">{borders}{fill}</a:tcPr></a:tc>'

    grid = "".join(f'<a:gridCol w="{col_w}"/>' for _ in columns)
    body = f'<a:tr h="{row_h}">' + "".join(cell(_label(column), header=True) for column in columns) + "</a:tr>"
    for row in rows:
        body += f'<a:tr h="{row_h}">' + "".join(cell(_cell_text(row.get(column)), header=False) for column in columns) + "</a:tr>"
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Table"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{col_w * len(columns)}" cy="{row_h * (len(rows) + 1)}"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f'<a:tbl><a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>{grid}</a:tblGrid>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    )


_PPTX_THEME = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Agent Saffron"><a:themeElements>'
    f'<a:clrScheme name="Saffron"><a:dk1><a:srgbClr val="{_INK}"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="3D3530"/></a:dk2><a:lt2><a:srgbClr val="FAF6F0"/></a:lt2>'
    f'<a:accent1><a:srgbClr val="{_ACCENT}"/></a:accent1><a:accent2><a:srgbClr val="B85A0A"/></a:accent2><a:accent3><a:srgbClr val="138808"/></a:accent3>'
    '<a:accent4><a:srgbClr val="000080"/></a:accent4><a:accent5><a:srgbClr val="F4B26B"/></a:accent5><a:accent6><a:srgbClr val="6B645C"/></a:accent6>'
    '<a:hlink><a:srgbClr val="B85A0A"/></a:hlink><a:folHlink><a:srgbClr val="8A4A12"/></a:folHlink></a:clrScheme>'
    '<a:fontScheme name="Saffron"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Saffron"><a:fillStyleLst>' + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 + '</a:fillStyleLst>'
    '<a:lnStyleLst>' + '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3 + '</a:lnStyleLst>'
    '<a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + '</a:effectStyleLst>'
    '<a:bgFillStyleLst>' + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 + '</a:bgFillStyleLst></a:fmtScheme>'
    '</a:themeElements></a:theme>'
)


def render_pptx(title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, subtitle: str | None = None) -> bytes:
    """A PowerPoint deck: a title slide, then the rows as tables of a dozen rows a slide."""

    shown_columns = list(columns[:MAX_PPTX_COLUMNS])
    shown = list(rows[:MAX_PPTX_ROWS])
    margin = 457200  # 0.5 inch
    about = _generated_line(len(rows), len(shown), subtitle)
    if len(shown_columns) < len(columns):
        about += f" Showing {len(shown_columns)} of {len(columns)} columns."
    slides = [_pptx_slide(
        _pptx_bar(2, 0, 0, 228600, _SLIDE_H)
        + _pptx_box(3, "Title", 914400, 1828800, _SLIDE_W - 1828800, 1600200, [_pptx_text(title, size=4000, color=_INK, bold=True)], anchor="b")
        + _pptx_box(5, "About", 914400, 3566160, _SLIDE_W - 1828800, 1371600, [_pptx_text(about, size=1600, color=_MUTED)])
    )]
    chunks = [shown[start:start + PPTX_ROWS_PER_SLIDE] for start in range(0, len(shown), PPTX_ROWS_PER_SLIDE)] or [[]]
    for index, chunk in enumerate(chunks):
        first = index * PPTX_ROWS_PER_SLIDE + 1
        heading = f"{title} — rows {first:,}–{first + len(chunk) - 1:,} of {len(rows):,}" if chunk else f"{title} — no rows"
        slides.append(_pptx_slide(
            _pptx_bar(2, 0, 0, _SLIDE_W, 91440)
            + _pptx_box(3, "Heading", margin, 274320, _SLIDE_W - 2 * margin, 640080, [_pptx_text(heading, size=2000, color=_INK, bold=True)], anchor="ctr")
            + _pptx_table(shown_columns, chunk, x=margin, y=1051560, width=_SLIDE_W - 2 * margin)
        ))
    count = len(slides)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
        + "".join(f'<Override PartName="/ppt/slides/slide{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for n in range(1, count + 1))
        + f"{_CORE_TYPE}</Types>"
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}slideMaster" Target="slideMasters/slideMaster1.xml"/><Relationship Id="rId2" Type="{rel}theme" Target="theme/theme1.xml"/>'
        f'<Relationship Id="rId3" Type="{rel}presProps" Target="presProps.xml"/><Relationship Id="rId4" Type="{rel}viewProps" Target="viewProps.xml"/>'
        f'<Relationship Id="rId5" Type="{rel}tableStyles" Target="tableStyles.xml"/>'
        + "".join(f'<Relationship Id="rId{n + 5}" Type="{rel}slide" Target="slides/slide{n}.xml"/>' for n in range(1, count + 1))
        + "</Relationships>"
    )
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}officeDocument" Target="ppt/presentation.xml"/>{_CORE_REL}</Relationships>'
        ),
        "ppt/presentation.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation {_P_NS} saveSubsetFonts="1">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>'
            + "".join(f'<p:sldId id="{255 + n}" r:id="rId{n + 5}"/>' for n in range(1, count + 1))
            + f'</p:sldIdLst><p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": presentation_rels,
        "ppt/slideMasters/slideMaster1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster {_P_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
            f"<p:spTree>{_EMPTY_TREE}</p:spTree></p:cSld>"
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
            '<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="3200"/></a:lvl1pPr></p:titleStyle><p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle>'
            '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>'
        ),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="{rel}theme" Target="../theme/theme1.xml"/></Relationships>'
        ),
        "ppt/slideLayouts/slideLayout1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout {_P_NS} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{_EMPTY_TREE}</p:spTree></p:cSld>'
            "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
        ),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>'
        ),
        "ppt/theme/theme1.xml": _PPTX_THEME,
        "ppt/presProps.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentationPr {_P_NS}/>',
        "ppt/viewProps.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:viewPr {_P_NS}/>',
        "ppt/tableStyles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>',
        "docProps/core.xml": _core_properties(title),
    }
    for n, slide in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{n}.xml"] = slide
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'
        )
    return _zip(parts)


FORMAT_CONTENT_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    # Files the open-task agent builds. HTML, SVG and scripts are never stored:
    # a download must not run in the reader's browser.
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
}


def render_report(format_name: str, title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], *, subtitle: str | None = None) -> tuple[bytes, str]:
    fmt = format_name.lower().strip()
    if fmt == "csv":
        return render_csv(columns, rows), FORMAT_CONTENT_TYPES["csv"]
    if fmt in {"xlsx", "excel"}:
        return render_xlsx(columns, rows, sheet_name=title[:31]), FORMAT_CONTENT_TYPES["xlsx"]
    if fmt == "pdf":
        return render_table_pdf(title, columns, rows, subtitle=subtitle), FORMAT_CONTENT_TYPES["pdf"]
    if fmt in {"docx", "word"}:
        return render_docx(title, columns, rows, subtitle=subtitle), FORMAT_CONTENT_TYPES["docx"]
    if fmt in {"pptx", "powerpoint"}:
        return render_pptx(title, columns, rows, subtitle=subtitle), FORMAT_CONTENT_TYPES["pptx"]
    raise ValueError("report format must be csv, xlsx, pdf, docx, or pptx")


__all__ = ["FORMAT_CONTENT_TYPES", "MAX_DOCX_ROWS", "MAX_PPTX_ROWS", "MAX_REPORT_ROWS", "render_csv", "render_docx", "render_pdf", "render_pptx", "render_report", "render_table_pdf", "render_xlsx"]
