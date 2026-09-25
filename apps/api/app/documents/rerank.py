"""Second-stage rerankers: score the top-K retrieved passages against the query.

Retrieval casts a wide net cheaply (top-K by vector similarity); a reranker
reads each candidate next to the question and keeps the best top-N for the
model. ``LexicalReranker`` needs no network: it scores candidates with BM25
over the candidate set plus bonuses for covering every query term and for
matching query phrases. ``HttpReranker`` calls a hosted cross-encoder through
the ``/rerank`` shape shared by Cohere, Jina, Voyage and LiteLLM gateways
(Hugging Face TEI's ``{index, score}`` answer is accepted too).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .embeddings import tokenize


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class Reranker(Protocol):
    provider_name: str

    async def rerank(self, query: str, passages: Sequence[str], *, top_n: int) -> list[RerankResult]: ...


def _bigrams(tokens: Sequence[str]) -> set[str]:
    return {f"{a}_{b}" for a, b in zip(tokens, tokens[1:])}


@dataclass(slots=True)
class LexicalReranker:
    k1: float = 1.2
    b: float = 0.75
    coverage_weight: float = 0.3
    phrase_weight: float = 0.2
    provider_name: str = "lexical-bm25"

    async def rerank(self, query: str, passages: Sequence[str], *, top_n: int) -> list[RerankResult]:
        query_tokens = tokenize(query)
        if not passages:
            return []
        if not query_tokens:
            return [RerankResult(index, 0.0) for index in range(min(top_n, len(passages)))]
        documents = [tokenize(text) for text in passages]
        average_length = sum(len(doc) for doc in documents) / len(documents) or 1.0
        document_frequency = Counter(token for doc in documents for token in set(doc))
        unique_query = list(dict.fromkeys(query_tokens))
        query_bigrams = _bigrams(query_tokens)
        raw: list[tuple[float, float, float]] = []
        for doc in documents:
            counts = Counter(doc)
            bm25 = 0.0
            for token in unique_query:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                idf = math.log(1 + (len(documents) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
                bm25 += idf * frequency * (self.k1 + 1) / (frequency + self.k1 * (1 - self.b + self.b * len(doc) / average_length))
            coverage = sum(1 for token in unique_query if token in counts) / len(unique_query)
            phrase = len(query_bigrams & _bigrams(doc)) / len(query_bigrams) if query_bigrams else 0.0
            raw.append((bm25, coverage, phrase))
        top_bm25 = max(item[0] for item in raw) or 1.0
        base = 1.0 - self.coverage_weight - self.phrase_weight
        scored = [RerankResult(index, round(base * bm25 / top_bm25 + self.coverage_weight * coverage + self.phrase_weight * phrase, 6)) for index, (bm25, coverage, phrase) in enumerate(raw)]
        # Stable sort keeps the retrieval order between equal scores.
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:max(1, top_n)]


@dataclass(slots=True)
class HttpReranker:
    base_url: str
    api_key: str = field(default="", repr=False)
    model_id: str = "rerank-v3.5"
    timeout_seconds: float = 30.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    provider_name: str = field(default="http-rerank", init=False)

    async def rerank(self, query: str, passages: Sequence[str], *, top_n: int) -> list[RerankResult]:
        if not passages:
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model_id, "query": query[:2000], "documents": [text[:4000] for text in passages], "top_n": min(max(1, top_n), len(passages))}
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(f"{self.base_url.rstrip('/')}/rerank", headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        items = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("rerank provider returned an invalid response")
        results: list[RerankResult] = []
        for item in items:
            index = item.get("index") if isinstance(item, dict) else None
            score = item.get("relevance_score", item.get("score")) if isinstance(item, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(passages) or not isinstance(score, (int, float)):
                raise ValueError("rerank provider returned an invalid result")
            results.append(RerankResult(index, float(score)))
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:max(1, top_n)]


__all__ = ["HttpReranker", "LexicalReranker", "RerankResult", "Reranker"]
