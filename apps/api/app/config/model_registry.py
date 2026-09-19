"""Provider metadata without embedding provider credentials."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderKind(StrEnum):
    DETERMINISTIC_DEMO = "deterministic_demo"
    HOSTED_TEXT = "hosted_text"
    LOCAL_OLLAMA = "local_ollama"
    REALTIME_VOICE = "realtime_voice"


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    provider_id: str
    provider_kind: ProviderKind
    model_id: str
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


__all__ = ["ModelDefinition", "ModelRegistry", "ProviderKind"]
