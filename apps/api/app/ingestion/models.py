"""Intermediate representation shared by every parser.

A parser never writes to the canonical database. It produces ``ParsedTable``
objects whose rows keep the institution's own headers untouched, plus the
locator (sheet, row, page) needed for lineage and audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FileKind(StrEnum):
    CSV = "csv"
    TSV = "tsv"
    XLSX = "xlsx"
    XLS = "xls"
    DOCX = "docx"
    PDF = "pdf"
    IMAGE = "image"
    JSON = "json"
    TEXT = "text"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class IntermediateRecord:
    """One source row with its provenance; ``fields`` keep the original headers."""

    source_file: str
    locator: str
    row_number: int
    fields: dict[str, Any]
    sheet: str | None = None
    page: int | None = None
    ocr: bool = False
    ocr_confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "sheet": self.sheet,
            "page": self.page,
            "row_number": self.row_number,
            "locator": self.locator,
            "ocr": self.ocr,
            "ocr_confidence": self.ocr_confidence,
            "fields": dict(self.fields),
        }


@dataclass(slots=True)
class ParsedTable:
    name: str
    headers: tuple[str, ...]
    records: list[IntermediateRecord]
    warnings: list[str] = field(default_factory=list)
    page: int | None = None
    ocr: bool = False
    # The text of the title lines above the header row ("MBA II SEM RESULT"), if any.
    title: str = ""

    @property
    def row_count(self) -> int:
        return len(self.records)


@dataclass(slots=True)
class ParsedText:
    """Free text extracted from a page or section; used for documents and OCR."""

    locator: str
    text: str
    page: int | None = None
    ocr: bool = False
    ocr_confidence: float | None = None


@dataclass(slots=True)
class ParseResult:
    file_name: str
    file_kind: FileKind
    tables: list[ParsedTable] = field(default_factory=list)
    texts: list[ParsedText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)

    def largest_table(self) -> ParsedTable | None:
        if not self.tables:
            return None
        return max(self.tables, key=lambda table: (table.row_count, len(table.headers)))

    def full_text(self) -> str:
        return "\n\n".join(item.text for item in self.texts if item.text.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "file_kind": self.file_kind.value,
            "page_count": self.page_count,
            "tables": [
                {"name": table.name, "headers": list(table.headers), "rows": table.row_count, "warnings": list(table.warnings), "ocr": table.ocr}
                for table in self.tables
            ],
            "text_sections": len(self.texts),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


class ParserError(ValueError):
    """Raised when a file cannot be parsed safely."""


class ParserUnavailable(ParserError):
    """Raised when the optional engine a format needs is not installed."""


__all__ = ["FileKind", "IntermediateRecord", "ParseResult", "ParsedTable", "ParsedText", "ParserError", "ParserUnavailable"]
