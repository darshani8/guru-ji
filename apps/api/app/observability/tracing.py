"""Redaction-safe tracing protocol for Agentic Saffron.

The recorder is deliberately backend-neutral. OTLP, Phoenix, Langfuse, or a
local collector can be added later without making any of them canonical for
provenance or authorization.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Mapping, Protocol
from uuid import uuid4


_ALLOWED_KEYS = frozenset({
    "request_id", "conversation_id", "principal_id", "principal_type", "scope_college_id",
    "scope_department_id", "scope_batch_id", "resource_id", "resource_type", "action",
    "tool_name", "source_id", "provider_id", "model_id", "status", "outcome", "latency_ms",
    "rows_used", "complete", "policy_version", "decision_id", "pdp_decision_ids", "redactions_applied",
    # Conversation turns: which path answered, in which language, over which channel.
    "route", "language", "channel",
})
_SENSITIVE_KEYS = frozenset({"prompt", "input", "output", "content", "text", "token", "secret", "authorization", "audio"})


@dataclass(frozen=True, slots=True)
class TraceSpan:
    trace_id: str
    span_id: str
    name: str
    started_at: datetime
    attributes: tuple[tuple[str, str | int | bool | None], ...]


class TraceExporterProtocol(Protocol):
    def export(self, span: TraceSpan) -> None: ...


class TraceRecorder:
    """Bounded in-process trace recorder with an explicit attribute allow-list."""

    def __init__(self, max_spans: int = 2_000, exporter: TraceExporterProtocol | None = None) -> None:
        if max_spans <= 0:
            raise ValueError("max_spans must be positive")
        self._spans: deque[TraceSpan] = deque(maxlen=max_spans)
        self._lock = RLock()
        self._exporter = exporter

    @staticmethod
    def _safe_attributes(attributes: Mapping[str, object]) -> tuple[tuple[str, str | int | bool | None], ...]:
        safe: list[tuple[str, str | int | bool | None]] = []
        for key, value in attributes.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _SENSITIVE_KEYS or normalized_key not in _ALLOWED_KEYS:
                continue
            if isinstance(value, (str, int, bool)) or value is None:
                safe.append((normalized_key, value))
            elif isinstance(value, (list, tuple, set)):
                safe.append((normalized_key, ",".join(str(item) for item in value)[:500]))
        return tuple(sorted(safe))

    def record(self, name: str, *, trace_id: str, attributes: Mapping[str, object] | None = None) -> TraceSpan:
        span = TraceSpan(
            trace_id=trace_id,
            span_id=f"span-{uuid4().hex}",
            name=name,
            started_at=datetime.now(timezone.utc),
            attributes=self._safe_attributes(attributes or {}),
        )
        with self._lock:
            self._spans.append(span)
        if self._exporter is not None:
            try:
                self._exporter.export(span)
            except Exception:  # noqa: BLE001 - telemetry is deliberately best effort
                pass
        return span

    def recent(self, limit: int = 100) -> tuple[TraceSpan, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            return tuple(list(self._spans)[-limit:][::-1])


__all__ = ["TraceExporterProtocol", "TraceRecorder", "TraceSpan"]
