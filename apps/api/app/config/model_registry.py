"""Provider metadata without embedding provider credentials."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..providers.model_base import ProviderCapabilities


class ProviderKind(StrEnum):
    DETERMINISTIC_DEMO = "deterministic_demo"
    HOSTED_TEXT = "hosted_text"
    LITELLM = "litellm"
    LOCAL_OLLAMA = "local_ollama"
    REALTIME_VOICE = "realtime_voice"


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    provider_id: str
    provider_kind: ProviderKind
    model_id: str
    capabilities: ProviderCapabilities = ProviderCapabilities()
    enabled: bool = False


class ModelRegistry:
    def __init__(self, definitions: tuple[ModelDefinition, ...] = ()) -> None:
        self._definitions = {item.provider_id: item for item in definitions}

    def register(self, definition: ModelDefinition) -> None:
        if definition.provider_id in self._definitions:
            raise ValueError(f"model provider already registered: {definition.provider_id}")
        self._definitions[definition.provider_id] = definition

    def get(self, provider_id: str) -> ModelDefinition:
        return self._definitions[provider_id]

    def enabled(self) -> tuple[ModelDefinition, ...]:
        return tuple(item for item in self._definitions.values() if item.enabled)

    def require(self, provider_id: str, *, streaming: bool = False, json_schema: bool = False) -> ModelDefinition:
        definition = self.get(provider_id)
        if not definition.enabled:
            raise ValueError(f"model provider is not enabled: {provider_id}")
        if streaming and not definition.capabilities.supports_streaming:
            raise ValueError(f"provider does not support streaming: {provider_id}")
        if json_schema and not definition.capabilities.supports_json_schema:
            raise ValueError(f"provider does not support JSON schema: {provider_id}")
        return definition


__all__ = ["ModelDefinition", "ModelRegistry", "ProviderKind"]
