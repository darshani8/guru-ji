"""Similarity search over stored chunk embeddings.

``InstitutionVectorStore`` reads the vectors that ingestion writes into the
``document_chunks`` table and ranks them with a hybrid score: cosine
similarity against the query embedding plus the share of query terms the
chunk contains. Visibility (classification) and category filters are applied
before any scoring, so a chunk the caller may not see never becomes a
candidate. Another backend (pgvector, OpenSearch) only has to implement
``VectorStore.search``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..institution_data.store import InstitutionDataStore
from .embeddings import cosine, tokenize

SEMANTIC_WEIGHT = 0.7
LEXICAL_WEIGHT = 0.3


@dataclass(frozen=True, slots=True)
class VectorHit:
    chunk: dict[str, Any]
    score: float
    semantic: float
    lexical: float


class VectorStore(Protocol):
    store_name: str

    def search(self, institution_id: str, query_vector: Sequence[float], query_text: str, *, classifications: Sequence[str], top_k: int, category: str | None = None) -> list[VectorHit]: ...


@dataclass(slots=True)
class InstitutionVectorStore:
    store: InstitutionDataStore
    semantic_weight: float = SEMANTIC_WEIGHT
    lexical_weight: float = LEXICAL_WEIGHT
    store_name: str = "institution-db"

    def search(self, institution_id: str, query_vector: Sequence[float], query_text: str, *, classifications: Sequence[str], top_k: int, category: str | None = None) -> list[VectorHit]:
        chunks = self.store.document_chunks(institution_id, classifications=classifications)
        if category:
            chunks = [chunk for chunk in chunks if chunk.get("category") == category.lower()]
        query_tokens = set(tokenize(query_text))
        hits: list[VectorHit] = []
        for chunk in chunks:
            semantic = cosine(query_vector, chunk["embedding"])
            lexical = len(query_tokens & set(tokenize(chunk["text"]))) / len(query_tokens) if query_tokens else 0.0
            score = self.semantic_weight * semantic + self.lexical_weight * lexical
            if score > 0:
                hits.append(VectorHit(chunk, score, semantic, lexical))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:max(1, top_k)]


__all__ = ["InstitutionVectorStore", "VectorHit", "VectorStore"]
