"""OCR engine boundary.

OCR output is never trusted automatically: the validator flags confusable
characters and low confidence, and rows from OCR carry an ``ocr`` marker into
lineage. Engines are adapters; the platform ships a Tesseract CLI adapter and
an AWS Textract adapter (boto3 optional) plus an explicit "not configured"
engine so a missing dependency is reported instead of silently returning
empty text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    lines: tuple[OcrLine, ...] = ()
    confidence: float | None = None
    engine: str = "none"
    tables: tuple[tuple[tuple[str, ...], ...], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.text.strip()


class OcrUnavailable(RuntimeError):
    """Raised when no OCR engine can process the image."""


class OcrEngine(Protocol):
    engine_name: str

    def recognize(self, image_bytes: bytes, *, content_type: str = "image/png") -> OcrResult: ...


class DisabledOcrEngine:
    engine_name = "disabled"

    def recognize(self, image_bytes: bytes, *, content_type: str = "image/png") -> OcrResult:
        raise OcrUnavailable("OCR is not configured; set GURU_OCR_ENGINE to tesseract or textract")


class TesseractCliOcrEngine:
    """Runs the ``tesseract`` binary; text and per-line confidence via TSV output."""

    engine_name = "tesseract"

    def __init__(self, binary: str = "tesseract", languages: str = "eng", timeout_seconds: float = 60.0) -> None:
        self.binary = binary
        self.languages = languages
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def recognize(self, image_bytes: bytes, *, content_type: str = "image/png") -> OcrResult:
        if not self.available():
            raise OcrUnavailable("tesseract binary is not installed")
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.img"
            image_path.write_bytes(image_bytes)
            try:
                completed = subprocess.run(  # noqa: S603 - fixed binary, no shell, temp file input
                    [self.binary, str(image_path), "stdout", "-l", self.languages, "tsv"],
                    capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OcrUnavailable("tesseract failed to run") from exc
        if completed.returncode != 0:
            raise OcrUnavailable("tesseract returned an error")
        return _parse_tesseract_tsv(completed.stdout)


def _parse_tesseract_tsv(tsv: str) -> OcrResult:
    lines: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
    for raw in tsv.splitlines()[1:]:
        parts = raw.split("\t")
        if len(parts) < 12:
            continue
        level, page, block, para, line = parts[0], parts[1], parts[2], parts[3], parts[4]
        if level != "5":
            continue
        try:
            confidence = float(parts[10])
        except ValueError:
            confidence = -1.0
        word = parts[11].strip()
        if not word or confidence < 0:
            continue
        lines.setdefault((page, block, para, line), []).append((word, confidence))
    ocr_lines: list[OcrLine] = []
    for words in lines.values():
        text = " ".join(word for word, _ in words)
        confidence = sum(conf for _, conf in words) / len(words) / 100.0
        ocr_lines.append(OcrLine(text=text, confidence=round(confidence, 3)))
    overall = round(sum(item.confidence or 0 for item in ocr_lines) / len(ocr_lines), 3) if ocr_lines else None
    return OcrResult(text="\n".join(item.text for item in ocr_lines), lines=tuple(ocr_lines), confidence=overall, engine="tesseract")


class TextractOcrEngine:
    """AWS Textract adapter (DetectDocumentText / AnalyzeDocument for tables)."""

    engine_name = "textract"

    def __init__(self, region: str | None = None, client: Any | None = None, *, analyze_tables: bool = True) -> None:
        if client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Textract OCR requires the optional boto3 dependency") from exc
            client = boto3.client("textract", region_name=region) if region else boto3.client("textract")
        self._client = client
        self.analyze_tables = analyze_tables

    def recognize(self, image_bytes: bytes, *, content_type: str = "image/png") -> OcrResult:
        try:
            if self.analyze_tables:
                response = self._client.analyze_document(Document={"Bytes": image_bytes}, FeatureTypes=["TABLES"])
            else:
                response = self._client.detect_document_text(Document={"Bytes": image_bytes})
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable("Textract request failed") from exc
        return parse_textract_blocks(response.get("Blocks", []))


def parse_textract_blocks(blocks: list[dict[str, Any]]) -> OcrResult:
    by_id = {block.get("Id"): block for block in blocks}
    lines: list[OcrLine] = []
    for block in blocks:
        if block.get("BlockType") == "LINE" and block.get("Text"):
            confidence = block.get("Confidence")
            lines.append(OcrLine(text=str(block["Text"]), confidence=round(float(confidence) / 100.0, 3) if confidence is not None else None))
    tables: list[tuple[tuple[str, ...], ...]] = []
    for block in blocks:
        if block.get("BlockType") != "TABLE":
            continue
        cells: dict[tuple[int, int], str] = {}
        for relationship in block.get("Relationships", []):
            if relationship.get("Type") != "CHILD":
                continue
            for cell_id in relationship.get("Ids", []):
                cell = by_id.get(cell_id)
                if not cell or cell.get("BlockType") != "CELL":
                    continue
                words: list[str] = []
                for cell_rel in cell.get("Relationships", []):
                    if cell_rel.get("Type") != "CHILD":
                        continue
                    for word_id in cell_rel.get("Ids", []):
                        word = by_id.get(word_id)
                        if word and word.get("BlockType") == "WORD" and word.get("Text"):
                            words.append(str(word["Text"]))
                cells[(int(cell.get("RowIndex", 1)), int(cell.get("ColumnIndex", 1)))] = " ".join(words)
        if not cells:
            continue
        max_row = max(row for row, _ in cells)
        max_col = max(col for _, col in cells)
        tables.append(tuple(tuple(cells.get((row, col), "") for col in range(1, max_col + 1)) for row in range(1, max_row + 1)))
    overall = round(sum(item.confidence or 0 for item in lines) / len(lines), 3) if lines else None
    return OcrResult(text="\n".join(item.text for item in lines), lines=tuple(lines), confidence=overall, engine="textract", tables=tuple(tables))


def ocr_result_from_json(payload: str) -> OcrResult:
    """Helper for tests and offline fixtures: {"text": ..., "confidence": ...}."""

    data = json.loads(payload)
    lines = tuple(OcrLine(text=str(item.get("text", "")), confidence=item.get("confidence")) for item in data.get("lines", []))
    return OcrResult(text=str(data.get("text", "")), lines=lines, confidence=data.get("confidence"), engine=str(data.get("engine", "fixture")))


__all__ = [
    "DisabledOcrEngine", "OcrEngine", "OcrLine", "OcrResult", "OcrUnavailable", "TesseractCliOcrEngine",
    "TextractOcrEngine", "ocr_result_from_json", "parse_textract_blocks",
]
