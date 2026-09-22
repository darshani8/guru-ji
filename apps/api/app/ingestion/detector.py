"""Decide what kind of file was uploaded from bytes first, extension second."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from .models import FileKind

_EXTENSIONS = {
    ".csv": FileKind.CSV,
    ".tsv": FileKind.TSV,
    ".txt": FileKind.TEXT,
    ".xlsx": FileKind.XLSX,
    ".xlsm": FileKind.XLSX,
    ".xls": FileKind.XLS,
    ".docx": FileKind.DOCX,
    ".pdf": FileKind.PDF,
    ".png": FileKind.IMAGE,
    ".jpg": FileKind.IMAGE,
    ".jpeg": FileKind.IMAGE,
    ".tif": FileKind.IMAGE,
    ".tiff": FileKind.IMAGE,
    ".bmp": FileKind.IMAGE,
    ".webp": FileKind.IMAGE,
    ".json": FileKind.JSON,
}

_CONTENT_TYPES = {
    "text/csv": FileKind.CSV,
    "text/tab-separated-values": FileKind.TSV,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileKind.XLSX,
    "application/vnd.ms-excel": FileKind.XLS,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileKind.DOCX,
    "application/pdf": FileKind.PDF,
    "image/png": FileKind.IMAGE,
    "image/jpeg": FileKind.IMAGE,
    "image/tiff": FileKind.IMAGE,
    "image/webp": FileKind.IMAGE,
    "application/json": FileKind.JSON,
    "text/plain": FileKind.TEXT,
}


def _zip_kind(content: bytes) -> FileKind | None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    if any(name.startswith("xl/") for name in names):
        return FileKind.XLSX
    if any(name.startswith("word/") for name in names):
        return FileKind.DOCX
    return None


def detect_file_kind(file_name: str, content: bytes, content_type: str | None = None) -> FileKind:
    head = content[:16]
    if head.startswith(b"%PDF"):
        return FileKind.PDF
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head[:4] in {b"II*\x00", b"MM\x00*"} or head.startswith(b"BM") or head[:4] == b"RIFF":
        return FileKind.IMAGE
    if head.startswith(b"PK\x03\x04"):
        kind = _zip_kind(content)
        if kind is not None:
            return kind
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return FileKind.XLS
    suffix = PurePosixPath(file_name.replace("\\", "/")).suffix.lower()
    if suffix in _EXTENSIONS:
        return _EXTENSIONS[suffix]
    if content_type:
        normalized = content_type.split(";")[0].strip().lower()
        if normalized in _CONTENT_TYPES:
            return _CONTENT_TYPES[normalized]
    sample = content[:4096]
    if b"\x00" in sample:
        return FileKind.UNKNOWN
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return FileKind.UNKNOWN
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    if text and printable / len(text) < 0.9:
        return FileKind.UNKNOWN
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return FileKind.JSON
    if "\t" in text and text.count("\t") >= text.count(","):
        return FileKind.TSV
    if "," in text:
        return FileKind.CSV
    return FileKind.TEXT if text.strip() else FileKind.UNKNOWN


__all__ = ["detect_file_kind"]
