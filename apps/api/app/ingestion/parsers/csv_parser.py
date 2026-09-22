"""CSV/TSV parsing with encoding and dialect detection."""

from __future__ import annotations

import csv
from io import StringIO

from ..models import FileKind, ParseResult, ParserError
from .tabular import MAX_ROWS, grid_to_table

_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")


def decode_text(content: bytes) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ParserError("text file encoding could not be determined")


def parse_csv(file_name: str, content: bytes, *, kind: FileKind = FileKind.CSV) -> ParseResult:
    text, encoding = decode_text(content)
    sample = text[:20_000]
    delimiter = "\t" if kind is FileKind.TSV else ","
    if kind is FileKind.CSV:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    try:
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_ROWS + 20:
                break
    except csv.Error as exc:
        # Typically an unbalanced quote that swallows the rest of the file into one field.
        raise ParserError(f"CSV could not be parsed near row {len(rows) + 1}: {exc}") from exc
    table = grid_to_table(rows, name="data", source_file=file_name, locator_prefix="csv")
    result = ParseResult(file_name=file_name, file_kind=kind, tables=[table], page_count=1, metadata={"encoding": encoding, "delimiter": delimiter})
    result.warnings.extend(table.warnings)
    return result


__all__ = ["decode_text", "parse_csv"]
