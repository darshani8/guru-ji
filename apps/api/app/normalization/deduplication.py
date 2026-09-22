"""Deterministic keys first, fuzzy matching second, humans for the rest."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..institution_data.models import CanonicalRecord

NAME_SIMILARITY_THRESHOLD = 0.9


@dataclass(slots=True)
class DuplicateCandidate:
    kind: str  # exact_key | conflicting_key | probable_person
    left_locator: str
    right_locator: str | None
    record_key: str | None
    score: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "left_locator": self.left_locator,
            "right_locator": self.right_locator,
            "record_key": self.record_key,
            "score": round(self.score, 3),
            "evidence": dict(self.evidence),
        }


def _name_similarity(left: Any, right: Any) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, str(left).lower(), str(right).lower()).ratio()


def _person_signals(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    evidence: dict[str, Any] = {}
    score = _name_similarity(left.get("name"), right.get("name"))
    evidence["name_similarity"] = round(score, 3)
    if score < NAME_SIMILARITY_THRESHOLD:
        return 0.0, evidence
    corroborating = 0
    for key in ("date_of_birth", "phone", "email", "guardian_phone"):
        if left.get(key) and right.get(key) and str(left[key]).lower() == str(right[key]).lower():
            corroborating += 1
            evidence[key] = "match"
    for key in ("program", "admission_year"):
        if left.get(key) and right.get(key) and str(left[key]).lower() == str(right[key]).lower():
            evidence[key] = "match"
    if corroborating:
        return min(1.0, score + 0.1 * corroborating), evidence
    return 0.0, evidence


def find_duplicates(records: Sequence[CanonicalRecord], existing: Mapping[str, Mapping[str, Any]]) -> tuple[list[DuplicateCandidate], dict[int, str]]:
    """Return duplicate candidates and per-record actions (insert/update/duplicate_in_batch/conflict)."""

    candidates: list[DuplicateCandidate] = []
    actions: dict[int, str] = {}
    first_by_key: dict[str, int] = {}
    people_fields = ("name", "date_of_birth", "phone", "email", "guardian_phone", "program", "admission_year")
    is_person = records and records[0].entity in {"student", "faculty", "staff", "admission"}
    for index, record in enumerate(records):
        key = record.record_key
        locator = record.lineage.source_locator or f"row={index + 1}"
        if not key.strip("|"):
            actions[index] = "missing_key"
            continue
        if key in first_by_key:
            other = records[first_by_key[key]]
            if other.content_hash == record.content_hash:
                actions[index] = "duplicate_in_batch"
                candidates.append(DuplicateCandidate("exact_key", locator, other.lineage.source_locator, key, 1.0, {"identical": True}))
            else:
                actions[index] = "conflict_in_batch"
                differing = sorted(name for name in set(record.fields) | set(other.fields) if record.fields.get(name) != other.fields.get(name))
                candidates.append(DuplicateCandidate("conflicting_key", locator, other.lineage.source_locator, key, 0.95, {"differing_fields": differing[:20]}))
            continue
        first_by_key[key] = index
        if key in existing:
            actions[index] = "update"
        else:
            actions[index] = "insert"
    if is_person:
        # Probable same person under a different or missing identifier.
        keyed = [(index, records[index]) for index in first_by_key.values()]
        for position, (index, record) in enumerate(keyed):
            left = {name: record.fields.get(name) for name in people_fields}
            for other_index, other in keyed[position + 1:]:
                right = {name: other.fields.get(name) for name in people_fields}
                score, evidence = _person_signals(left, right)
                if score >= NAME_SIMILARITY_THRESHOLD:
                    candidates.append(DuplicateCandidate("probable_person", record.lineage.source_locator or f"row={index + 1}", other.lineage.source_locator, other.record_key, score, evidence))
            for existing_key, existing_record in existing.items():
                if existing_key == record.record_key:
                    continue
                right = {name: existing_record.get(name) for name in people_fields}
                score, evidence = _person_signals(left, right)
                if score >= NAME_SIMILARITY_THRESHOLD:
                    evidence["existing_record_key"] = existing_key
                    candidates.append(DuplicateCandidate("probable_person", record.lineage.source_locator or f"row={index + 1}", None, existing_key, score, evidence))
    return candidates, actions


__all__ = ["DuplicateCandidate", "NAME_SIMILARITY_THRESHOLD", "find_duplicates"]
