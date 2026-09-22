"""Split extracted text into overlapping chunks that remember their page."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..ingestion.models import ParsedText

DEFAULT_CHUNK_CHARS = 900
DEFAULT_OVERLAP_CHARS = 150
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n{2,}")


@dataclass(frozen=True, slots=True)
class TextChunk:
    chunk_index: int
    text: str
    page_number: int | None
    locator: str

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // 4)


def _clean(text: str) -> str:
    """Collapse whitespace per line but keep one blank line between paragraphs."""

    cleaned: list[str] = []
    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            cleaned.append(collapsed)
        elif cleaned and cleaned[-1] != "":
            cleaned.append("")
    return "\n".join(cleaned).strip()


def chunk_texts(sections: Sequence[ParsedText], *, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS, max_chunks: int = 5_000) -> list[TextChunk]:
    if chunk_chars <= 0 or overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("invalid chunking parameters")
    chunks: list[TextChunk] = []
    for section in sections:
        text = _clean(section.text)
        if not text:
            continue
        pieces = [piece for piece in _SENTENCE_BOUNDARY.split(text) if piece and piece.strip()]
        buffer = ""
        for piece in pieces:
            candidate = f"{buffer} {piece}".strip() if buffer else piece.strip()
            # A short buffer is usually a heading; keep it with the paragraph it introduces.
            if len(candidate) <= chunk_chars or len(buffer) < 80:
                buffer = candidate
            else:
                chunks.append(TextChunk(len(chunks), buffer, section.page, section.locator))
                tail = buffer[-overlap_chars:] if overlap_chars else ""
                buffer = f"{tail} {piece}".strip()
            while len(buffer) > chunk_chars:
                chunks.append(TextChunk(len(chunks), buffer[:chunk_chars], section.page, section.locator))
                buffer = buffer[chunk_chars - overlap_chars:]
            if len(chunks) >= max_chunks:
                return chunks
        if buffer:
            chunks.append(TextChunk(len(chunks), buffer, section.page, section.locator))
        if len(chunks) >= max_chunks:
            break
    return chunks


__all__ = ["DEFAULT_CHUNK_CHARS", "DEFAULT_OVERLAP_CHARS", "TextChunk", "chunk_texts"]
