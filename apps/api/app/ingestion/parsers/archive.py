"""Guards for zip-based office formats (.xlsx, .docx are zipped XML).

An archive of a few hundred kilobytes can inflate to gigabytes (a "zip
bomb"), and the parsers run inside the API process. Every entry is checked
against the declared sizes before anything is inflated, and every read goes
through a byte budget so a lying header cannot get past the cap either.
"""

from __future__ import annotations

import zipfile
from typing import IO

from ..models import ParserError

# Total (and single-entry) inflated size an archive may declare.
MAX_INFLATED_BYTES = 200_000_000
# Entries above this size must not inflate more than MAX_COMPRESSION_RATIO times.
RATIO_CHECK_MIN_BYTES = 1_000_000
MAX_COMPRESSION_RATIO = 100


def check_archive_limits(archive: zipfile.ZipFile, *, max_bytes: int | None = None, max_ratio: int = MAX_COMPRESSION_RATIO) -> int:
    """Reject archives whose declared inflated size or compression ratio is implausible.

    Returns the total declared inflated size. Must run before any entry is read.
    """

    limit = MAX_INFLATED_BYTES if max_bytes is None else max_bytes
    total = 0
    for info in archive.infolist():
        if info.file_size > limit:
            raise ParserError(f"archive entry {info.filename} inflates to {info.file_size} bytes, above the {limit} byte limit")
        total += info.file_size
        if total > limit:
            raise ParserError(f"archive inflates to more than {limit} bytes")
        if info.file_size > RATIO_CHECK_MIN_BYTES and info.file_size > max_ratio * max(info.compress_size, 1):
            raise ParserError(f"archive entry {info.filename} has an implausible compression ratio")
    return total


class BoundedReader:
    """A read-only file wrapper that fails once more than ``budget`` bytes were delivered."""

    def __init__(self, source: IO[bytes], budget: int, name: str = "archive entry") -> None:
        self._source = source
        self._budget = int(budget)
        self._delivered = 0
        self._name = name

    @property
    def delivered(self) -> int:
        return self._delivered

    def close(self) -> None:
        self._source.close()

    def __enter__(self) -> BoundedReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._budget - self._delivered + 1
        data = self._source.read(size)
        self._delivered += len(data)
        if self._delivered > self._budget:
            raise ParserError(f"{self._name} inflates beyond the {self._budget} byte limit")
        return data


def read_entry(archive: zipfile.ZipFile, name: str, *, max_bytes: int | None = None) -> bytes:
    """Read one entry fully, bounded by ``max_bytes`` (defaults to MAX_INFLATED_BYTES).

    Raises ``KeyError`` when the entry is absent (callers decide whether that
    is fatal) and ``ParserError`` when it is corrupt or over budget.
    """

    limit = MAX_INFLATED_BYTES if max_bytes is None else max_bytes
    try:
        with archive.open(name) as raw:
            reader = BoundedReader(raw, limit, name)
            chunks: list[bytes] = []
            while True:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
    except zipfile.BadZipFile as exc:
        raise ParserError(f"archive entry {name} is corrupt") from exc
    return b"".join(chunks)


def open_entry(archive: zipfile.ZipFile, name: str, *, max_bytes: int | None = None) -> BoundedReader:
    """Open one entry for streaming under a byte budget; ``KeyError`` when absent."""

    limit = MAX_INFLATED_BYTES if max_bytes is None else max_bytes
    return BoundedReader(archive.open(name), limit, name)


__all__ = ["BoundedReader", "MAX_COMPRESSION_RATIO", "MAX_INFLATED_BYTES", "RATIO_CHECK_MIN_BYTES", "check_archive_limits", "open_entry", "read_entry"]
