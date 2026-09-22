"""PDF parsing that decides page by page between text extraction and OCR.

Engines in preference order: PyMuPDF (``fitz``), ``pdfplumber``, ``pypdf``.
Pages with no extractable text are rendered and sent to the configured OCR
engine when one is available; otherwise the page is reported as needing OCR
so nothing is silently dropped.
"""

from __future__ import annotations

from typing import Any

from ..models import FileKind, IntermediateRecord, ParseResult, ParsedTable, ParsedText, ParserError, ParserUnavailable
from .ocr import DisabledOcrEngine, OcrEngine, OcrUnavailable
from .tabular import grid_to_table
from .text_layout import key_value_fields, text_to_table

MIN_TEXT_CHARS_PER_PAGE = 25
MAX_PAGES = 400


def _pages_with_fitz(content: bytes) -> tuple[list[dict[str, Any]], str] | None:
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError:
        return None
    pages: list[dict[str, Any]] = []
    with fitz.open(stream=content, filetype="pdf") as document:
        for index, page in enumerate(document, start=1):
            if index > MAX_PAGES:
                break
            entry: dict[str, Any] = {"number": index, "text": page.get_text("text") or "", "tables": [], "image": None}
            try:
                finder = page.find_tables()
                for table in finder.tables:
                    entry["tables"].append(table.extract())
            except Exception:  # noqa: BLE001 - table detection is best effort
                pass
            if len(entry["text"].strip()) < MIN_TEXT_CHARS_PER_PAGE:
                try:
                    pixmap = page.get_pixmap(dpi=200)
                    entry["image"] = pixmap.tobytes("png")
                except Exception:  # noqa: BLE001
                    entry["image"] = None
            pages.append(entry)
    return pages, "pymupdf"


def _pages_with_pdfplumber(content: bytes) -> tuple[list[dict[str, Any]], str] | None:
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError:
        return None
    from io import BytesIO

    pages: list[dict[str, Any]] = []
    with pdfplumber.open(BytesIO(content)) as document:
        for index, page in enumerate(document.pages, start=1):
            if index > MAX_PAGES:
                break
            entry: dict[str, Any] = {"number": index, "text": page.extract_text() or "", "tables": [], "image": None}
            try:
                entry["tables"] = page.extract_tables() or []
            except Exception:  # noqa: BLE001
                entry["tables"] = []
            if len(entry["text"].strip()) < MIN_TEXT_CHARS_PER_PAGE:
                try:
                    entry["image"] = page.to_image(resolution=200).original.tobytes()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    entry["image"] = None
            pages.append(entry)
    return pages, "pdfplumber"


def _pages_with_pypdf(content: bytes) -> tuple[list[dict[str, Any]], str] | None:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return None
    from io import BytesIO

    reader = PdfReader(BytesIO(content))
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        if index > MAX_PAGES:
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        pages.append({"number": index, "text": text, "tables": [], "image": None})
    return pages, "pypdf"


def extract_pdf_pages(content: bytes) -> tuple[list[dict[str, Any]], str]:
    if not content.startswith(b"%PDF"):
        raise ParserError("file is not a PDF")
    for extractor in (_pages_with_fitz, _pages_with_pdfplumber, _pages_with_pypdf):
        try:
            result = extractor(content)
        except ParserError:
            raise
        except Exception:  # noqa: BLE001 - try the next engine
            continue
        if result is not None:
            return result
    raise ParserUnavailable("PDF parsing needs one of PyMuPDF, pdfplumber, or pypdf installed (see the 'ingest' extra)")


def parse_pdf(file_name: str, content: bytes, *, ocr_engine: OcrEngine | None = None) -> ParseResult:
    pages, engine = extract_pdf_pages(content)
    ocr = ocr_engine or DisabledOcrEngine()
    result = ParseResult(file_name=file_name, file_kind=FileKind.PDF, page_count=len(pages), metadata={"engine": engine, "ocr_pages": 0, "text_pages": 0})
    for page in pages:
        number = int(page["number"])
        text = str(page.get("text") or "")
        used_ocr = False
        confidence: float | None = None
        if len(text.strip()) < MIN_TEXT_CHARS_PER_PAGE:
            image = page.get("image")
            if image is None:
                result.warnings.append(f"page_{number}_needs_ocr")
                continue
            try:
                ocr_result = ocr.recognize(image)
            except OcrUnavailable as exc:
                result.warnings.append(f"page_{number}_needs_ocr:{exc}")
                continue
            text = ocr_result.text
            confidence = ocr_result.confidence
            used_ocr = True
            result.metadata["ocr_pages"] += 1
            for table_index, grid in enumerate(ocr_result.tables, start=1):
                table = grid_to_table([list(row) for row in grid], name=f"page_{number}_table_{table_index}", source_file=file_name, page=number, ocr=True, ocr_confidence=confidence)
                if table.headers:
                    result.tables.append(table)
        else:
            result.metadata["text_pages"] += 1
        for table_index, grid in enumerate(page.get("tables") or [], start=1):
            rows = [["" if cell is None else str(cell) for cell in row] for row in grid if row]
            table = grid_to_table(rows, name=f"page_{number}_table_{table_index}", source_file=file_name, page=number)
            if table.headers and table.records:
                result.tables.append(table)
        if not any(table.page == number for table in result.tables):
            layout_table = text_to_table(text, name=f"page_{number}_layout", source_file=file_name, page=number, ocr=used_ocr, ocr_confidence=confidence)
            if layout_table is not None:
                layout_table.warnings.append("table_inferred_from_text_layout")
                result.tables.append(layout_table)
        if text.strip():
            result.texts.append(ParsedText(locator=f"page={number}", text=text, page=number, ocr=used_ocr, ocr_confidence=confidence))
    if not result.tables and result.texts:
        form_records = _form_records(file_name, result)
        if form_records:
            result.tables.append(ParsedTable(name="form_fields", headers=tuple(sorted({key for record in form_records for key in record.fields})), records=form_records, warnings=["fields_extracted_from_form_layout"]))
    return result


def _form_records(file_name: str, result: ParseResult) -> list[IntermediateRecord]:
    records: list[IntermediateRecord] = []
    for text in result.texts:
        fields = key_value_fields(text.text)
        if len(fields) >= 3:
            records.append(IntermediateRecord(
                source_file=file_name, locator=text.locator, row_number=text.page or len(records) + 1, fields=fields,
                page=text.page, ocr=text.ocr, ocr_confidence=text.ocr_confidence,
            ))
    return records


__all__ = ["extract_pdf_pages", "parse_pdf"]
