"""Resolve "$step.path" references so later steps can consume earlier results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_REFERENCE = re.compile(r"^\$(?P<step>[A-Za-z0-9_-]+)(?P<path>(?:\.[A-Za-z0-9_]+|\[\*\]|\[\d+\])*)$")
_SEGMENT = re.compile(r"\.([A-Za-z0-9_]+)|\[(\*|\d+)\]")


class BindingError(ValueError):
    """A reference could not be resolved against completed step results."""


def is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$") and _REFERENCE.match(value) is not None


def resolve_reference(reference: str, results: Mapping[str, Any]) -> Any:
    match = _REFERENCE.match(reference)
    if match is None:
        raise BindingError(f"invalid reference: {reference}")
    step_id = match.group("step")
    if step_id not in results:
        raise BindingError(f"reference to unknown or incomplete step: {step_id}")
    current: Any = results[step_id]
    for segment in _SEGMENT.finditer(match.group("path") or ""):
        key, index = segment.group(1), segment.group(2)
        if key is not None:
            if isinstance(current, list):
                current = [item.get(key) if isinstance(item, Mapping) else None for item in current]
            elif isinstance(current, Mapping):
                current = current.get(key)
            else:
                raise BindingError(f"cannot read {key} from a scalar in {reference}")
        elif index == "*":
            if not isinstance(current, list):
                current = [] if current is None else [current]
        else:
            if not isinstance(current, list):
                raise BindingError(f"cannot index a non-list in {reference}")
            position = int(index)
            current = current[position] if position < len(current) else None
    return current


def resolve_arguments(arguments: Mapping[str, Any], bindings: Mapping[str, str], results: Mapping[str, Any]) -> dict[str, Any]:
    resolved = dict(arguments)
    for name, reference in bindings.items():
        value = resolve_reference(reference, results)
        if isinstance(value, list):
            value = [item for item in value if item is not None]
        resolved[name] = value
    return resolved



def referenced_steps(step: Any) -> set[str]:
    """The steps a plan step waits for or reads from."""

    found = set(step.depends_on)
    for value in (*step.bindings.values(), *step.arguments.values()):
        match = _REFERENCE.match(value) if isinstance(value, str) else None
        if match is not None:
            found.add(match.group("step"))
    return found


__all__ = ["BindingError", "is_reference", "referenced_steps", "resolve_arguments", "resolve_reference"]
