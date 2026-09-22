"""Route a file to its parser by detected kind."""

from __future__ import annotations

from dataclasses import dataclass, field

from .detector import detect_file_kind
from .models import FileKind, ParseResult, ParserUnavailable
from .parsers.csv_parser import parse_csv
from .parsers.docx_parser import parse_docx
from .parsers.excel_parser import parse_xls, parse_xlsx
from .parsers.image_parser import parse_image
from .parsers.json_parser import parse_json, parse_text
from .parsers.ocr import DisabledOcrEngine, OcrEngine
from .parsers.pdf_parser import parse_pdf


@dataclass(slots=True)
class ParserRegistry:
    ocr_engine: OcrEngine = field(default_factory=DisabledOcrEngine)
    max_bytes: int = 50_000_000

    def supported_kinds(self) -> tuple[FileKind, ...]:
        return (FileKind.CSV, FileKind.TSV, FileKind.XLSX, FileKind.XLS, FileKind.DOCX, FileKind.PDF, FileKind.IMAGE, FileKind.JSON, FileKind.TEXT)

    def parse(self, file_name: str, content: bytes, *, content_type: str | None = None, kind: FileKind | None = None) -> ParseResult:
        if len(content) > self.max_bytes:
            raise ValueError("file exceeds the configured ingestion size limit")
        if not content:
            raise ValueError("file is empty")
        detected = kind or detect_file_kind(file_name, content, content_type)
        if detected in {FileKind.CSV, FileKind.TSV}:
            return parse_csv(file_name, content, kind=detected)
        if detected is FileKind.XLSX:
            return parse_xlsx(file_name, content)
        if detected is FileKind.XLS:
            return parse_xls(file_name, content)
        if detected is FileKind.DOCX:
            return parse_docx(file_name, content)
        if detected is FileKind.PDF:
            return parse_pdf(file_name, content, ocr_engine=self.ocr_engine)
        if detected is FileKind.IMAGE:
            return parse_image(file_name, content, ocr_engine=self.ocr_engine, content_type=content_type or "image/png")
        if detected is FileKind.JSON:
            return parse_json(file_name, content)
        if detected is FileKind.TEXT:
            return parse_text(file_name, content)
        raise ParserUnavailable(f"unsupported file kind: {detected.value}")


__all__ = ["ParserRegistry"]
