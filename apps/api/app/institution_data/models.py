"""Records that cross the ingestion -> canonical store boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..normalization.canonical import CanonicalEntity, entity as canonical_entity


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _key_part(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return " ".join(text.split())


def record_key_for(entity: CanonicalEntity, fields: dict[str, Any]) -> str:
    parts = [_key_part(fields.get(name)) for name in entity.natural_key]
    return "|".join(parts)


@dataclass(frozen=True, slots=True)
class RecordLineage:
    """Where a canonical record came from; answers "where did this come from?"."""

    source_file_id: str | None = None
    source_file_name: str | None = None
    source_locator: str | None = None
    ingestion_job_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "source_file_id": self.source_file_id,
            "source_file_name": self.source_file_name,
            "source_locator": self.source_locator,
            "ingestion_job_id": self.ingestion_job_id,
        }


@dataclass(slots=True)
class CanonicalRecord:
    entity: str
    fields: dict[str, Any]
    attributes: dict[str, Any] = field(default_factory=dict)
    normalizations: tuple[str, ...] = ()
    issues: tuple[dict[str, Any], ...] = ()
    lineage: RecordLineage = field(default_factory=RecordLineage)

    @property
    def definition(self) -> CanonicalEntity:
        return canonical_entity(self.entity)

    @property
    def record_key(self) -> str:
        return record_key_for(self.definition, self.fields)

    @property
    def content_hash(self) -> str:
        payload = _canonical_json({"fields": self.fields, "attributes": self.attributes})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def has_blocking_issues(self) -> bool:
        return any(item.get("severity") == "error" for item in self.issues)


@dataclass(slots=True)
class ImportSummary:
    entity: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    inserted_keys: list[str] = field(default_factory=list)
    updated_keys: list[str] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    # Records skipped for a reason only the store sees (merged with the
    # stored row they fail a check), with that reason and the issues.
    skipped_keys: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "inserted_keys_sample": self.inserted_keys[:50],
            "updated_keys_sample": self.updated_keys[:50],
            "skipped_reasons": dict(self.skipped_reasons),
            "skipped_keys_sample": list(self.skipped_keys[:50]),
        }


__all__ = ["CanonicalRecord", "ImportSummary", "RecordLineage", "record_key_for"]
