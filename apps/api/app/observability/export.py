"""Small, dependency-light trace export adapters.

The exporter receives already-filtered ``TraceSpan`` values. It never accepts
prompts, answer text, tokens, raw source rows, or audio, and export failures do
not change an authorization or answer decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .tracing import TraceSpan


class TraceExporter(Protocol):
    def export(self, span: TraceSpan) -> None: ...


@dataclass(frozen=True, slots=True)
class HttpJsonTraceExporter:
    endpoint: str
    timeout_seconds: float = 2.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("trace exporter endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("trace exporter endpoint must not contain credentials, query, or fragment data")
        if self.timeout_seconds <= 0:
            raise ValueError("trace exporter timeout must be positive")

    def export(self, span: TraceSpan) -> None:
        payload = {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "name": span.name,
            "started_at": span.started_at.isoformat(),
            "attributes": {key: value for key, value in span.attributes},
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.endpoint, json=payload, headers={"Content-Type": "application/json"})
                response.raise_for_status()
        except httpx.HTTPError:
            # Observability must remain best-effort and cannot turn a safe
            # answer into an unsafe retry or an availability incident.
            return


__all__ = ["HttpJsonTraceExporter", "TraceExporter"]
