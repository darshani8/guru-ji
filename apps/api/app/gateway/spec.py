"""Platform tool specifications with closed argument schemas.

A tool is a named, documented, permission-bound function. The planner (model
or deterministic) may only pick tools from the registry and may only pass
arguments that validate against the spec; anything else is rejected before
policy is even consulted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..domain.principals import Capability, InstitutionScope, Principal
from ..policy.query_limits import QueryLimits


class RiskLevel(StrEnum):
    READ = "read"          # no side effects
    WRITE = "write"        # creates artifacts, messages, or notifications
    HIGH_RISK = "high_risk"  # changes institutional records; requires explicit approval


_PDP_ACTION = {RiskLevel.READ: "retrieve", RiskLevel.WRITE: "execute", RiskLevel.HIGH_RISK: "high_risk"}


class ToolArgumentError(ValueError):
    """Raised when arguments do not satisfy the tool's schema."""


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    type: str  # string | integer | number | boolean | array | object
    description: str
    required: bool = False
    enum: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 500
    items_type: str = "string"
    default: Any = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.type == "string":
            schema["maxLength"] = self.max_length
        if self.type == "array":
            schema["items"] = {"type": self.items_type}
            schema["maxItems"] = self.max_length
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    request_id: str
    principal: Principal
    scope: InstitutionScope
    channel: str = "text"
    limits: QueryLimits = field(default_factory=QueryLimits)
    approval_id: str | None = None
    conversation_id: str | None = None

    @property
    def institution_id(self) -> str:
        return self.scope.college_id


@dataclass(slots=True)
class ToolOutput:
    data: Any
    summary: str = ""
    warnings: list[dict[str, str]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    records_returned: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)


ToolHandler = Callable[[ToolCallContext, dict[str, Any]], Awaitable[ToolOutput]]
# Optional semantic check that runs after schema validation and authorization
# but before any approval is created or the handler runs. It returns the
# normalized arguments (the approval digest is computed on them) or raises
# ValueError with a message safe to show the caller.
ToolValidator = Callable[[ToolCallContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PlatformToolSpec:
    name: str
    description: str
    group: str
    required_capability: Capability
    handler: ToolHandler = field(repr=False, compare=False)
    parameters: tuple[ParameterSpec, ...] = ()
    risk: RiskLevel = RiskLevel.READ
    returns: str = ""
    allowed_fields: tuple[str, ...] = ()
    audit_policy: str = "metadata_only"
    examples: tuple[str, ...] = ()
    validator: ToolValidator | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.name.replace("_", "").isalnum() or self.name != self.name.lower():
            raise ValueError("tool names must be lowercase snake_case")
        names = [item.name for item in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate parameter in tool {self.name}")

    @property
    def pdp_action(self) -> str:
        return _PDP_ACTION[self.risk]

    def json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "risk": self.risk.value,
            "permission": self.required_capability.value,
            "parameters": {
                "type": "object",
                "properties": {item.name: item.json_schema() for item in self.parameters},
                "required": [item.name for item in self.parameters if item.required],
                "additionalProperties": False,
            },
            "returns": self.returns,
        }

    def validate_arguments(self, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(arguments or {})
        known = {item.name: item for item in self.parameters}
        unknown = sorted(set(supplied) - set(known))
        if unknown:
            raise ToolArgumentError(f"unknown argument(s) for {self.name}: {', '.join(unknown)}")
        result: dict[str, Any] = {}
        for spec in self.parameters:
            value = supplied.get(spec.name, spec.default)
            if value is None or value == "":
                if spec.required:
                    raise ToolArgumentError(f"{self.name} requires argument {spec.name}")
                continue
            result[spec.name] = _coerce(spec, value)
        return result


def _coerce(spec: ParameterSpec, value: Any) -> Any:
    if spec.type == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ToolArgumentError(f"{spec.name} must be a string")
        value = value.strip()
        if len(value) > spec.max_length:
            raise ToolArgumentError(f"{spec.name} exceeds {spec.max_length} characters")
        if spec.enum and value.lower() not in {item.lower() for item in spec.enum}:
            raise ToolArgumentError(f"{spec.name} must be one of: {', '.join(spec.enum)}")
        return value
    if spec.type == "integer":
        if isinstance(value, bool):
            raise ToolArgumentError(f"{spec.name} must be an integer")
        if isinstance(value, str):
            try:
                value = int(value.strip().rstrip("%"))
            except ValueError as exc:
                raise ToolArgumentError(f"{spec.name} must be an integer") from exc
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ToolArgumentError(f"{spec.name} must be an integer")
        _check_bounds(spec, value)
        return value
    if spec.type == "number":
        if isinstance(value, bool):
            raise ToolArgumentError(f"{spec.name} must be a number")
        if isinstance(value, str):
            try:
                value = float(value.strip().rstrip("%"))
            except ValueError as exc:
                raise ToolArgumentError(f"{spec.name} must be a number") from exc
        if not isinstance(value, (int, float)):
            raise ToolArgumentError(f"{spec.name} must be a number")
        _check_bounds(spec, float(value))
        return float(value)
    if spec.type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}:
            return False
        raise ToolArgumentError(f"{spec.name} must be a boolean")
    if spec.type == "array":
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, (list, tuple)):
            raise ToolArgumentError(f"{spec.name} must be an array")
        if len(value) > spec.max_length:
            raise ToolArgumentError(f"{spec.name} exceeds {spec.max_length} items")
        items: list[Any] = []
        for item in value:
            if spec.items_type == "string":
                if not isinstance(item, (str, int, float)) or isinstance(item, bool):
                    raise ToolArgumentError(f"{spec.name} items must be strings")
                items.append(str(item).strip()[:500])
            elif spec.items_type == "object":
                if not isinstance(item, Mapping):
                    raise ToolArgumentError(f"{spec.name} items must be objects")
                items.append(dict(item))
            else:
                items.append(item)
        return items
    if spec.type == "object":
        if not isinstance(value, Mapping):
            raise ToolArgumentError(f"{spec.name} must be an object")
        if len(value) > spec.max_length:
            raise ToolArgumentError(f"{spec.name} has too many keys")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ToolArgumentError(f"{spec.name} keys must be non-empty strings")
            if isinstance(item, (Mapping, list, tuple, set, frozenset)):
                raise ToolArgumentError(f"{spec.name}.{key} must be a scalar value, not a nested object or list")
            result[key] = item
        return result
    raise ToolArgumentError(f"unsupported parameter type for {spec.name}")


def _check_bounds(spec: ParameterSpec, value: float) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise ToolArgumentError(f"{spec.name} must be at least {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise ToolArgumentError(f"{spec.name} must be at most {spec.maximum}")


def param(name: str, type_: str, description: str, *, required: bool = False, enum: Sequence[str] = (), minimum: float | None = None, maximum: float | None = None, max_length: int = 500, items_type: str = "string", default: Any = None) -> ParameterSpec:
    return ParameterSpec(name, type_, description, required, tuple(enum), minimum, maximum, max_length, items_type, default)


__all__ = ["ParameterSpec", "PlatformToolSpec", "RiskLevel", "ToolArgumentError", "ToolCallContext", "ToolHandler", "ToolOutput", "ToolValidator", "param"]
