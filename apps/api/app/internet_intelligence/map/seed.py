"""Seed the map from a manual sweep, and set aside the ground truth it is measured against.

A sweep row is a claim, not a fact: it is imported as ``imported_claim``
evidence, which the grader weighs below anything the map observes itself,
so every seeded asset still has to be re-verified. A fixed share of rows is
held out: those become hidden ground truth and are never seeded, so the
map's ability to find them again honestly measures whether it "reaches more".
Lookalikes become entities a page must beat, and the ones with a URL become
canaries the map must never grade as official.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .assets import asset_ref
from .store import GRADE_RANK, RELATIONS, MapStore

SEEDS_DIR = Path(__file__).with_name("seeds")
DEFAULT_SWEEP = SEEDS_DIR / "sweep_2026-09-22.tsv"
DEFAULT_LOOKALIKES = SEEDS_DIR / "lookalikes.tsv"
_CLAIMED = frozenset({"A", "A-arch", "B", "C", "F"})


@dataclass(frozen=True, slots=True)
class SeedRow:
    group: str
    subject: str
    subject_kind: str
    parent: str
    label: str
    url: str
    claimed_grade: str
    relation: str
    note: str

    @property
    def expected_min_grade(self) -> str:
        """What re-verification should reach: official claims at least B, everything else found at all (C)."""

        return "B" if self.claimed_grade in {"A", "A-arch", "B"} and self.relation == "official" else "C"


@dataclass(frozen=True, slots=True)
class Lookalike:
    name: str
    names: tuple[str, ...]
    locations: tuple[str, ...]
    url: str
    why: str


@dataclass(slots=True)
class SeedSummary:
    entities: int = 0
    assets_created: int = 0
    assets_existing: int = 0
    evidence: int = 0
    holdout: int = 0
    canaries: int = 0
    lookalikes: int = 0
    skipped_groups: int = 0
    suppressed: int = 0
    canary_collisions: list[str] = field(default_factory=list)  # sweep rows that are look-alikes, not seeded
    groups: list[str] = field(default_factory=list)  # the groups actually imported
    invalid: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "entities": self.entities, "assets_created": self.assets_created, "assets_existing": self.assets_existing, "evidence": self.evidence, "holdout": self.holdout,
            "canaries": self.canaries, "lookalikes": self.lookalikes, "skipped_groups": self.skipped_groups, "suppressed": self.suppressed,
            "canary_collisions": self.canary_collisions[:50], "groups": self.groups, "invalid": self.invalid[:50],
        }


def _rows(text: str) -> list[list[str]]:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return [row for row in csv.reader(io.StringIO("\n".join(lines)), delimiter="\t")]


def parse_sweep(text: str) -> list[SeedRow]:
    rows = _rows(text)
    if not rows:
        return []
    header = [column.strip() for column in rows[0]]
    expected = ["group", "subject", "subject_kind", "parent", "label", "url", "claimed_grade", "relation", "note"]
    if header != expected:
        raise ValueError(f"sweep header must be {' / '.join(expected)}")
    parsed: list[SeedRow] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(expected):
            raise ValueError(f"sweep row {number} has {len(row)} columns, expected {len(expected)}")
        item = SeedRow(*(value.strip() for value in row))
        if item.claimed_grade not in _CLAIMED:
            raise ValueError(f"sweep row {number}: unknown claimed grade {item.claimed_grade!r}")
        if item.relation not in RELATIONS:
            raise ValueError(f"sweep row {number}: unknown relation {item.relation!r}")
        parsed.append(item)
    return parsed


def parse_lookalikes(text: str) -> list[Lookalike]:
    rows = _rows(text)
    if not rows:
        return []
    if [column.strip() for column in rows[0]] != ["name", "names", "locations", "url", "why"]:
        raise ValueError("lookalike header must be name / names / locations / url / why")
    return [
        Lookalike(row[0].strip(), tuple(item.strip() for item in row[1].split("|") if item.strip()), tuple(item.strip() for item in row[2].split("|") if item.strip()), row[3].strip(), row[4].strip())
        for row in rows[1:]
        if row and row[0].strip()
    ]


def in_holdout(asset_key: str, percent: int) -> bool:
    """Deterministic: the same key is held out on every import, so reseeding never leaks it."""

    return int(hashlib.sha256(asset_key.encode("utf-8")).hexdigest()[:8], 16) % 100 < max(0, min(percent, 90))


def import_seed(
    store: MapStore, institution_id: str, rows: Sequence[SeedRow], *, lookalikes: Iterable[Lookalike] = (), groups: Sequence[str] | None = None, holdout_percent: int = 20,
    source: str = "sweep-2026-09-22",
) -> SeedSummary:
    summary = SeedSummary()
    wanted = {group.strip().lower() for group in groups} if groups else None
    entity_ids: dict[tuple[str, str], str] = {}

    def entity(name: str, kind: str, group: str, parent: str = "") -> str:
        key = (kind, name)
        if key not in entity_ids:
            parent_id = entity("" if not parent else parent, _parent_kind(parent), group) if parent and parent != name else None
            entity_ids[key] = store.upsert_entity(institution_id, name=name, kind=kind, group_label=group, parent_id=parent_id)
            summary.entities += 1
        return entity_ids[key]

    # Look-alikes first: a sweep row for a look-alike's URL must neither be
    # seeded as an ordinary asset nor take the key's gold slot from the canary.
    canary_keys: set[str] = set()
    for lookalike in lookalikes:
        store.upsert_entity(institution_id, name=lookalike.name, kind="lookalike", names=lookalike.names, locations=lookalike.locations, authority="external", notes=lookalike.why)
        summary.lookalikes += 1
        if not lookalike.url:
            continue
        try:
            ref = asset_ref(lookalike.url)
        except ValueError:
            summary.invalid.append(lookalike.url)
            continue
        canary_keys.add(ref.key)
        if store.add_gold(institution_id, asset_key=ref.key, platform=ref.platform, split="canary", entity_name=lookalike.name, relation="third_party", expected_min_grade="D", source=source) or store.make_canary(institution_id, ref.key, entity_name=lookalike.name):
            summary.canaries += 1
        existing = store.find_asset(institution_id, ref.key)
        if existing is not None and not any(item["kind"] == "lookalike" for item in store.evidence_for(institution_id, [existing["asset_id"]])[existing["asset_id"]]):
            # Seeded earlier as an ordinary asset: it is a look-alike, and says so in its evidence.
            store.add_evidence(institution_id, asset_id=existing["asset_id"], kind="lookalike", polarity="refutes", detail=lookalike.why[:200] or lookalike.name, channel=source, observed_via="import")
    for row in rows:
        if wanted is not None and row.group.lower() not in wanted:
            summary.skipped_groups += 1
            continue
        try:
            ref = asset_ref(row.url)
        except ValueError:
            summary.invalid.append(row.url)
            continue
        if ref.key in canary_keys:
            summary.canary_collisions.append(ref.key)
            continue
        if store.is_suppressed(institution_id, ref.key):
            summary.suppressed += 1
            continue
        if row.group not in summary.groups:
            summary.groups.append(row.group)
        entity_id = entity(row.subject, row.subject_kind if row.subject_kind in _KINDS else "institution", row.group, row.parent)
        expected = row.expected_min_grade
        if in_holdout(ref.key, holdout_percent):
            if store.add_gold(institution_id, asset_key=ref.key, platform=ref.platform, split="holdout", entity_name=row.subject, relation=row.relation, expected_min_grade=expected, source=source):
                summary.holdout += 1
            continue
        store.add_gold(institution_id, asset_key=ref.key, platform=ref.platform, split="seed", entity_name=row.subject, relation=row.relation, expected_min_grade=expected, source=source)
        label = row.label if row.label != row.subject else ""
        asset_id, created = store.upsert_asset(institution_id, ref, entity_id=entity_id, relation=row.relation, note=" · ".join(part for part in (label, row.note) if part))
        summary.assets_created += int(created)
        summary.assets_existing += int(not created)
        store.add_evidence(institution_id, asset_id=asset_id, kind="imported_claim", detail=f"claimed {row.claimed_grade}: {row.note}", channel=source, observed_via="import", source_url="")
        summary.evidence += 1
    return summary


_KINDS = frozenset({"organisation", "trust", "institution", "unit", "branch", "role"})


def _parent_kind(name: str) -> str:
    lowered = name.lower()
    if "math" in lowered or "mutt" in lowered:
        return "organisation"
    if "group of institutions" in lowered:
        return "trust"
    return "institution"


def load_default_seed() -> tuple[list[SeedRow], list[Lookalike]]:
    return parse_sweep(DEFAULT_SWEEP.read_text(encoding="utf-8")), parse_lookalikes(DEFAULT_LOOKALIKES.read_text(encoding="utf-8"))


__all__ = ["DEFAULT_LOOKALIKES", "DEFAULT_SWEEP", "GRADE_RANK", "Lookalike", "SeedRow", "SeedSummary", "import_seed", "in_holdout", "load_default_seed", "parse_lookalikes", "parse_sweep"]
