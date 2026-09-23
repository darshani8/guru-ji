"""Whose word settles what the map says about an entity.

One institution's map also holds other institutions' entities: the BGSCET
pilot maps the Math and the Swamiji so it can tell their accounts from
impostors. Mapping them is not speaking for them. Which of the Swamiji's two
X accounts is real is for the Math's (or its trust's) IT office to say, not a
BGSCET reviewer, and a BGSCET page cannot settle it either. Every entity has
an ``authority``: 'self' for the institution's own, otherwise the sweep group
that owns it ('external' for a look-alike). An entity comes under every
authority on its parent chain, so a Math branch is the Math's too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

SELF = "self"
_MAX_DEPTH = 12  # parent chains are short; this only guards a cycle in bad data

EntityLookup = Callable[[str], Mapping[str, Any] | None]


def authorities(lookup: EntityLookup, entity_id: str | None) -> list[str]:
    """Every authority other than 'self' on the entity's parent chain, nearest first ([] for the institution's own)."""

    found: list[str] = []
    seen: set[str] = set()
    current = entity_id
    while current and current not in seen and len(seen) < _MAX_DEPTH:
        seen.add(current)
        entity = lookup(current)
        if entity is None:
            break
        authority = str(entity.get("authority") or SELF)
        if authority != SELF and authority not in found:
            found.append(authority)
        current = entity.get("parent_id")
    return found


def within(lookup: EntityLookup, entity_id: str | None, ancestor_id: str | None) -> bool:
    """Whether the entity is ``ancestor_id`` or one of its children (at any depth)."""

    seen: set[str] = set()
    current = entity_id
    while current and current not in seen and len(seen) < _MAX_DEPTH:
        if current == ancestor_id:
            return True
        seen.add(current)
        entity = lookup(current)
        current = entity.get("parent_id") if entity else None
    return False


def members(mapping: Mapping[str, Sequence[str]], authority: str) -> tuple[str, ...]:
    """The configured entries for an authority; group labels match whatever their case."""

    wanted = authority.strip().lower()
    return tuple(value for key, values in mapping.items() if key.strip().lower() == wanted for value in values)


def approves(approvers: Mapping[str, Sequence[str]], chain: Iterable[str], principal_id: str) -> str | None:
    """The authority on the chain the principal is a named approver for, if any."""

    return next((authority for authority in chain if principal_id in members(approvers, authority)), None)


__all__ = ["SELF", "approves", "authorities", "members", "within"]
