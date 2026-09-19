"""Explicit registry of approved source metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..policy.data_classification import DataClassification


class SourceLifecycleStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    institution_id: str
    display_name: str
    connector_type: str
    allowed_tools: tuple[str, ...]
    classification: DataClassification = DataClassification.CONFIDENTIAL
    status: SourceLifecycleStatus = SourceLifecycleStatus.ACTIVE
    timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        for field_name in ("source_id", "institution_id", "display_name", "connector_type"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        classification = self.classification
        if isinstance(classification, str):
            classification = DataClassification(classification)
        object.__setattr__(self, "classification", classification)
        status = self.status
        if isinstance(status, str):
            status = SourceLifecycleStatus(status)
        object.__setattr__(self, "status", status)


class SourceRegistry:
    def __init__(self, definitions: tuple[SourceDefinition, ...] = ()) -> None:
        self._definitions: dict[str, SourceDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SourceDefinition) -> None:
        if definition.source_id in self._definitions:
            raise ValueError(f"source already registered: {definition.source_id}")
        self._definitions[definition.source_id] = definition

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._definitions[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown source: {source_id}") from exc

    def active(self) -> tuple[SourceDefinition, ...]:
        return tuple(item for item in self._definitions.values() if item.status is SourceLifecycleStatus.ACTIVE)

    def for_institution(self, institution_id: str) -> tuple[SourceDefinition, ...]:
        return tuple(item for item in self.active() if item.institution_id == institution_id)

    def all(self) -> tuple[SourceDefinition, ...]:
        return tuple(self._definitions.values())


__all__ = ["SourceDefinition", "SourceLifecycleStatus", "SourceRegistry"]
