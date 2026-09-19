"""Versioned policy metadata for semantic tools."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.principals import Capability
from ..policy.data_classification import DataClassification, DisclosureLevel


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    tool_name: str
    required_capability: Capability
    classification: DataClassification
    disclosure_level: DisclosureLevel = DisclosureLevel.AGGREGATE
    policy_version: str = "2026-09-19"


class PolicyRegistry:
    def __init__(self, policies: tuple[ToolPolicy, ...] = ()) -> None:
        self._policies = {policy.tool_name: policy for policy in policies}

    def register(self, policy: ToolPolicy) -> None:
        if policy.tool_name in self._policies:
            raise ValueError(f"policy already registered: {policy.tool_name}")
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolPolicy:
        return self._policies[tool_name]

    def all(self) -> tuple[ToolPolicy, ...]:
        return tuple(self._policies.values())


__all__ = ["PolicyRegistry", "ToolPolicy"]
