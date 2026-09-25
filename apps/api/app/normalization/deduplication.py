"""Deterministic keys first, fuzzy matching second, humans for the rest.

Fuzzy person matching never compares every row with every other row. Records
are indexed into blocks by a normalised phone, email, date of birth and a
name key; only records sharing a block are compared, and the (expensive) name
similarity runs only after a corroborating field already matched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..institution_data.models import CanonicalRecord

NAME_SIMILARITY_THRESHOLD = 0.9
# A value shared by more than this many people (a college landline in the phone
# column, a placeholder date of birth, one guardian number for a hostel)
# identifies nobody: its block is not compared and it does not corroborate.
MAX_BLOCK_SIZE = 200
# Above this many keyed rows the in-batch pairwise pass is skipped (and reported);
# matching against existing records still runs since it is linear in the batch.
MAX_PAIRWISE_ROWS = 5000

PERSON_FIELDS = ("name", "date_of_birth", "phone", "email", "guardian_phone", "program", "admission_year")
CORROBORATING_FIELDS = ("date_of_birth", "phone", "email", "guardian_phone")
CONTEXT_FIELDS = ("program", "admission_year")


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


def _norm(value: Any) -> str:
    return str(value).strip().lower() if value not in (None, "") else ""


def _name_similarity(left: Any, right: Any) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, str(left).lower(), str(right).lower()).ratio()


def _person_signals(left: Mapping[str, Any], right: Mapping[str, Any], ignored: frozenset[str] | set[str] = frozenset()) -> tuple[float, dict[str, Any]]:
    """Score two people: a corroborating field must match before names are compared."""

    evidence: dict[str, Any] = {}
    corroborating = 0
    for key in CORROBORATING_FIELDS:
        if left.get(key) and right.get(key) and _norm(left[key]) == _norm(right[key]) and f"{key}:{_norm(left[key])}" not in ignored:
            corroborating += 1
            evidence[key] = "match"
    if not corroborating:
        return 0.0, evidence
    score = _name_similarity(left.get("name"), right.get("name"))
    evidence["name_similarity"] = round(score, 3)
    if score < NAME_SIMILARITY_THRESHOLD:
        return 0.0, evidence
    for key in CONTEXT_FIELDS:
        if left.get(key) and right.get(key) and _norm(left[key]) == _norm(right[key]):
            evidence[key] = "match"
    return min(1.0, score + 0.1 * corroborating), evidence


def _block_keys(fields: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for name in CORROBORATING_FIELDS:
        value = _norm(fields.get(name))
        if value:
            keys.add(f"{name}:{value}")
    tokens = _norm(fields.get("name")).split()
    if tokens:
        keys.add(f"name:{tokens[0]}|{tokens[-1]}")
    return keys


def _person_view(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields.get(name) for name in PERSON_FIELDS}


def find_duplicates(records: Sequence[CanonicalRecord], existing: Mapping[str, Mapping[str, Any]]) -> tuple[list[DuplicateCandidate], dict[int, str], list[str]]:
    """Return duplicate candidates, per-record actions (insert/update/duplicate_in_batch/conflict), and warnings."""

    candidates: list[DuplicateCandidate] = []
    actions: dict[int, str] = {}
    warnings: list[str] = []
    first_by_key: dict[str, int] = {}
    is_person = bool(records) and records[0].entity in {"student", "faculty", "staff", "admission"}
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
        candidates.extend(_probable_people(records, list(first_by_key.values()), existing, warnings))
    return candidates, actions, warnings


def _probable_people(records: Sequence[CanonicalRecord], keyed: Sequence[int], existing: Mapping[str, Mapping[str, Any]], warnings: list[str]) -> list[DuplicateCandidate]:
    """Probable same person under a different or missing identifier, found through blocks."""

    views = {index: _person_view(records[index].fields) for index in keyed}
    # Block entries: ("batch", record index) or ("existing", record key).
    blocks: dict[str, list[tuple[str, Any]]] = {}
    compare_batch = len(keyed) <= MAX_PAIRWISE_ROWS
    if compare_batch:
        for index in keyed:
            for block in _block_keys(views[index]):
                blocks.setdefault(block, []).append(("batch", index))
    else:
        warnings.append(f"probable_person_check_skipped_in_batch: {len(keyed)} rows exceed the {MAX_PAIRWISE_ROWS} row limit; rows were still checked against existing records")
    existing_views = {key: _person_view(fields) for key, fields in existing.items()}
    for key, view in existing_views.items():
        for block in _block_keys(view):
            blocks.setdefault(block, []).append(("existing", key))
    # Degenerate blocks would make the pass quadratic again and would flag
    # unrelated people who merely share a placeholder value.
    ignored = frozenset(block for block, members in blocks.items() if len(members) > MAX_BLOCK_SIZE)
    if ignored:
        fields = sorted({block.split(":", 1)[0] for block in ignored})
        warnings.append(f"shared_values_ignored: {', '.join(fields)} values shared by more than {MAX_BLOCK_SIZE} people were not used for matching")
    found: list[DuplicateCandidate] = []
    for index in keyed:
        record = records[index]
        left = views[index]
        locator = record.lineage.source_locator or f"row={index + 1}"
        # A row that updates a record already on file creates no new identity:
        # asking whether it is someone else could only drop the update.
        updates_existing = record.record_key in existing
        batch_hits: dict[int, tuple[float, dict[str, Any]]] = {}
        existing_hits: dict[str, tuple[float, dict[str, Any]]] = {}
        for block in _block_keys(left):
            if block in ignored:
                continue
            for kind, other in blocks.get(block, ()):
                if kind == "batch":
                    # Each in-batch pair is scored once, from the earlier row.
                    if other <= index or other in batch_hits or (updates_existing and records[other].record_key in existing):
                        continue
                    score, evidence = _person_signals(left, views[other], ignored)
                    if score >= NAME_SIMILARITY_THRESHOLD:
                        batch_hits[other] = (score, evidence)
                else:
                    if updates_existing or other == record.record_key or other in existing_hits:
                        continue
                    score, evidence = _person_signals(left, existing_views[other], ignored)
                    if score >= NAME_SIMILARITY_THRESHOLD:
                        existing_hits[other] = (score, evidence)
        for other_index in sorted(batch_hits):
            score, evidence = batch_hits[other_index]
            other = records[other_index]
            found.append(DuplicateCandidate("probable_person", locator, other.lineage.source_locator, other.record_key, score, evidence))
        for existing_key in sorted(existing_hits):
            score, evidence = existing_hits[existing_key]
            evidence["existing_record_key"] = existing_key
            found.append(DuplicateCandidate("probable_person", locator, None, existing_key, score, evidence))
    return found


__all__ = ["DuplicateCandidate", "MAX_BLOCK_SIZE", "MAX_PAIRWISE_ROWS", "NAME_SIMILARITY_THRESHOLD", "find_duplicates"]
