"""Provider protocol; providers receive already-authorized context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    supports_streaming: bool = False
    supports_json_schema: bool = False
    supports_tools: bool = False
    supports_multimodal: bool = False
    supports_realtime: bool = False
    max_context_tokens: int | None = None
    cost_metadata: str | None = None


@dataclass(frozen=True, slots=True)
class ModelEvent:
    """Provider-neutral model output; public API serialization is separate."""

    type: str
    text: str = ""
    is_final: bool = False
    provider_id: str = ""
    model_id: str = ""


class TextModel(Protocol):
    provider_id: str
    model_id: str
    capabilities: ProviderCapabilities

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str: ...

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]: ...


__all__ = ["ModelEvent", "ProviderCapabilities", "TextModel"]
