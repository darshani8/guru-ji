"""Control-plane metadata contracts.

These records intentionally contain identifiers, decisions, counts, hashes, and
provider signals only. Prompts, learner text, credentials, SQL rows, raw tool
results, embeddings, and raw audio are never part of these contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AnswerEnvelopeMetadata:
    request_id: str
    conversation_id: str | None
    principal_id: str | None
    college_id: str | None
    status: str
    citations_count: int
    warnings_count: int
    answer_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ModelAttemptMetadata:
    request_id: str
    provider_id: str
    model_id: str
    outcome: str
    latency_ms: int
    fallback: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    delivered_at: datetime | None = None


class ControlPlaneMetadataStore(Protocol):
    def record_answer_envelope(self, envelope: AnswerEnvelopeMetadata) -> None: ...

    def record_model_attempt(self, attempt: ModelAttemptMetadata) -> None: ...

    def enqueue_outbox(self, event_type: str, payload: dict[str, Any]) -> str: ...

    def drain_outbox(self, limit: int = 100) -> tuple[OutboxRecord, ...]: ...

    def ack_outbox(self, outbox_ids: tuple[str, ...]) -> None: ...

    def prune_retention(self, retention_days: int) -> int: ...


def answer_hash(answer: str) -> str:
    """Hash answer text for duplicate/review correlation without persistence."""

    return sha256(answer.encode("utf-8")).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AnswerEnvelopeMetadata",
    "ControlPlaneMetadataStore",
    "ModelAttemptMetadata",
    "OutboxRecord",
    "answer_hash",
    "now_utc",
]
