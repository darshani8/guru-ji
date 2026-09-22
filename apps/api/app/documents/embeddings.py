"""Embedding providers behind one protocol.

``HashingEmbeddingProvider`` needs no network or model: it hashes word and
bigram features into a fixed vector, which is enough for lexical retrieval in
tests and small pilots. Ollama and OpenAI-compatible (LiteLLM gateway)
providers are explicit adapters for real deployments.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "of", "and", "or", "to", "in", "for", "is", "are", "be", "on", "by", "with", "as", "at", "that", "this", "it", "from", "will", "shall", "must", "may", "not"})


class EmbeddingProvider(Protocol):
    provider_name: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    return [token for token in _TOKEN.findall(text.lower()) if token not in _STOP and len(token) > 1]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


@dataclass(slots=True)
class HashingEmbeddingProvider:
    dimensions: int = 512
    provider_name: str = "hashing-local"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = tokenize(text)
        features = list(tokens) + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 0.5 if "_" in feature else 1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


@dataclass(slots=True)
class OllamaEmbeddingProvider:
    base_url: str
    model_id: str = "nomic-embed-text"
    timeout_seconds: float = 30.0
    dimensions: int = 768
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    provider_name: str = field(default="ollama-embeddings", init=False)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            for text in texts:
                response = await client.post(f"{self.base_url.rstrip('/')}/api/embeddings", json={"model": self.model_id, "prompt": text[:8000]})
                response.raise_for_status()
                payload = response.json()
                vector = payload.get("embedding")
                if not isinstance(vector, list):
                    raise ValueError("embedding provider returned an invalid vector")
                vectors.append([float(value) for value in vector])
        if vectors:
            self.dimensions = len(vectors[0])
        return vectors


@dataclass(slots=True)
class OpenAICompatibleEmbeddingProvider:
    base_url: str
    api_key: str = field(repr=False)
    model_id: str = "text-embedding-3-small"
    timeout_seconds: float = 30.0
    dimensions: int = 1536
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    provider_name: str = field(default="openai-compatible-embeddings", init=False)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/embeddings", headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model_id, "input": [text[:8000] for text in texts]},
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("embedding provider returned an invalid response")
        vectors = [[float(value) for value in item.get("embedding", [])] for item in sorted(data, key=lambda item: item.get("index", 0))]
        if vectors:
            self.dimensions = len(vectors[0])
        return vectors


__all__ = ["EmbeddingProvider", "HashingEmbeddingProvider", "OllamaEmbeddingProvider", "OpenAICompatibleEmbeddingProvider", "cosine", "tokenize"]
