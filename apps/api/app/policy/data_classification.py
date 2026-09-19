"""Data classification and disclosure decisions."""

from enum import StrEnum


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DisclosureLevel(StrEnum):
    AGGREGATE = "aggregate"
    ROW = "row"
    IDENTIFIER = "identifier"


_ALLOWED: dict[DataClassification, frozenset[DisclosureLevel]] = {
    DataClassification.PUBLIC: frozenset(DisclosureLevel),
    DataClassification.INTERNAL: frozenset({DisclosureLevel.AGGREGATE, DisclosureLevel.ROW}),
    DataClassification.CONFIDENTIAL: frozenset({DisclosureLevel.AGGREGATE}),
    DataClassification.RESTRICTED: frozenset(),
}


def can_disclose(classification: DataClassification, level: DisclosureLevel) -> bool:
    if isinstance(classification, str):
        classification = DataClassification(classification)
    if isinstance(level, str):
        level = DisclosureLevel(level)
    return level in _ALLOWED[classification]


__all__ = ["DataClassification", "DisclosureLevel", "can_disclose"]
