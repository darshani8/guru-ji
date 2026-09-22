"""Word document parsing with the standard library (a .docx is zipped XML)."""

from __future__ import annotations

import zipfile
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

from ..models import FileKind, ParseResult, ParsedText, ParserError
from .tabular import grid_to_table

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W}


def _paragraph_text(node: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        tag = child.tag
        if tag == f"{{{_W}}}t" and child.text:
            parts.append(child.text)
        elif tag == f"{{{_W}}}tab":
            parts.append("\t")
        elif tag == f"{{{_W}}}br":
            parts.append("\n")
    return "".join(parts)


def _paragraph_style(node: ElementTree.Element) -> str:
    style = node.find("w:pPr/w:pStyle", _NS)
    return (style.get(f"{{{_W}}}val") or "") if style is not None else ""


def parse_docx(file_name: str, content: bytes) -> ParseResult:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParserError("document is not a valid .docx archive") from exc
    with archive:
        try:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        except (KeyError, ElementTree.ParseError) as exc:
            raise ParserError("document body could not be read") from exc
        body = root.find("w:body", _NS)
    result = ParseResult(file_name=file_name, file_kind=FileKind.DOCX, page_count=1)
    if body is None:
        return result
    paragraphs: list[str] = []
    headings: list[str] = []
    table_index = 0
    for element in body:
        if element.tag == f"{{{_W}}}p":
            text = _paragraph_text(element).strip()
            if not text:
                continue
            style = _paragraph_style(element).lower()
            if style.startswith("heading") or style == "title":
                headings.append(text)
                paragraphs.append(f"\n{text}\n")
            else:
                paragraphs.append(text)
        elif element.tag == f"{{{_W}}}tbl":
            table_index += 1
            rows: list[list[Any]] = []
            for row in element.findall("w:tr", _NS):
                rows.append([" ".join(_paragraph_text(p) for p in cell.findall("w:p", _NS)).strip() for cell in row.findall("w:tc", _NS)])
            table = grid_to_table(rows, name=f"table_{table_index}", source_file=file_name, locator_prefix=f"table={table_index}")
            result.tables.append(table)
            if not table.headers:
                paragraphs.append("\n".join(" | ".join(cell for cell in row) for row in rows))
    if paragraphs:
        result.texts.append(ParsedText(locator="body", text="\n".join(paragraphs), page=1))
    result.metadata = {"headings": headings[:50], "tables": table_index}
    return result


__all__ = ["parse_docx"]
