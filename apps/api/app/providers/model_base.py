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


class ProviderCapabilityError(ValueError):
    """Raised when a requested model feature is not supported by the provider."""


@dataclass(frozen=True, slots=True)
class ModelEvent:
    """Provider-neutral model output; public API serialization is separate."""

    type: str
    text: str = ""
    is_final: bool = False
    provider_id: str = ""
    model_id: str = ""
    usage: dict[str, int | float] | None = None


def require_capabilities(
    capabilities: ProviderCapabilities,
    *,
    streaming: bool = False,
    json_schema: bool = False,
    tools: bool = False,
    multimodal: bool = False,
    realtime: bool = False,
) -> None:
    requested = {
        "streaming": (streaming, capabilities.supports_streaming),
        "json_schema": (json_schema, capabilities.supports_json_schema),
        "tools": (tools, capabilities.supports_tools),
        "multimodal": (multimodal, capabilities.supports_multimodal),
        "realtime": (realtime, capabilities.supports_realtime),
    }
    unsupported = [name for name, (wanted, supported) in requested.items() if wanted and not supported]
    if unsupported:
        raise ProviderCapabilityError(f"provider does not support: {', '.join(unsupported)}")


class TextModel(Protocol):
    provider_id: str
    model_id: str
    capabilities: ProviderCapabilities

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str: ...

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]: ...


__all__ = ["ModelEvent", "ProviderCapabilities", "ProviderCapabilityError", "TextModel", "require_capabilities"]
