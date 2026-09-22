"""Scanned forms and photographed registers go through OCR, then layout analysis."""

from __future__ import annotations

from ..models import FileKind, IntermediateRecord, ParseResult, ParsedTable, ParsedText, ParserUnavailable
from .ocr import DisabledOcrEngine, OcrEngine, OcrUnavailable
from .tabular import grid_to_table
from .text_layout import key_value_fields, text_to_table


def parse_image(file_name: str, content: bytes, *, ocr_engine: OcrEngine | None = None, content_type: str = "image/png") -> ParseResult:
    engine = ocr_engine or DisabledOcrEngine()
    try:
        ocr = engine.recognize(content, content_type=content_type)
    except OcrUnavailable as exc:
        raise ParserUnavailable(str(exc)) from exc
    result = ParseResult(file_name=file_name, file_kind=FileKind.IMAGE, page_count=1, metadata={"engine": ocr.engine, "ocr_confidence": ocr.confidence})
    if ocr.empty:
        result.warnings.append("ocr_returned_no_text")
        return result
    result.texts.append(ParsedText(locator="page=1", text=ocr.text, page=1, ocr=True, ocr_confidence=ocr.confidence))
    for index, grid in enumerate(ocr.tables, start=1):
        table = grid_to_table([list(row) for row in grid], name=f"table_{index}", source_file=file_name, page=1, ocr=True, ocr_confidence=ocr.confidence)
        if table.headers:
            result.tables.append(table)
    if not result.tables:
        layout_table = text_to_table(ocr.text, name="layout", source_file=file_name, page=1, ocr=True, ocr_confidence=ocr.confidence)
        if layout_table is not None:
            layout_table.warnings.append("table_inferred_from_text_layout")
            result.tables.append(layout_table)
    if not result.tables:
        fields = key_value_fields(ocr.text)
        if len(fields) >= 3:
            record = IntermediateRecord(source_file=file_name, locator="page=1", row_number=1, fields=fields, page=1, ocr=True, ocr_confidence=ocr.confidence)
            result.tables.append(ParsedTable(name="form_fields", headers=tuple(fields), records=[record], warnings=["fields_extracted_from_form_layout"], page=1, ocr=True))
    if ocr.confidence is not None and ocr.confidence < 0.75:
        result.warnings.append("low_ocr_confidence")
    return result


__all__ = ["parse_image"]
