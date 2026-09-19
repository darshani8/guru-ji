"""Guru Ji policy decision point boundary.

This is intentionally a small local PDP with Cerbos-shaped inputs and outputs.
A future Cerbos sidecar can implement the same protocol without changing the
connector or orchestration contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from .authorization import DenialReason


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    action: str
    resource_type: str
    resource_id: str
    policy_version: str
    decision_id: str
    matched_scope: InstitutionScope | None = None
    reason: DenialReason | str | None = None

    def __post_init__(self) -> None:
        for name in ("action", "resource_type", "resource_id", "policy_version", "decision_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if self.allowed and self.reason is not None:
            raise ValueError("allowed policy decisions cannot contain a reason")
        if not self.allowed and self.reason is None:
            raise ValueError("denied policy decisions require a reason")


class PolicyDecisionPoint(Protocol):
    policy_version: str

    def evaluate(
        self,
        *,
        principal: Principal,
        required_capability: Capability,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
    ) -> PolicyDecision: ...


class LocalPolicyDecisionPoint:
    """Small fail-closed PDP used until a reviewed Cerbos sidecar is deployed."""

    policy_version = "guru-local-v1"
    _allowed_actions = frozenset({"list", "search", "retrieve", "mcp.tool.call"})

    def evaluate(
        self,
        *,
        principal: Principal,
        required_capability: Capability,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
    ) -> PolicyDecision:
        decision_id = f"pdp-{uuid4().hex}"
        if action not in self._allowed_actions:
            return PolicyDecision(
                allowed=False,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                policy_version=self.policy_version,
                decision_id=decision_id,
                reason="unknown_action",
            )
        if not principal.authenticated or principal.principal_type is PrincipalType.ANONYMOUS:
            reason: DenialReason | str = DenialReason.UNAUTHENTICATED
            matched_scope = None
        elif not principal.has_capability(required_capability):
            reason = DenialReason.MISSING_CAPABILITY
            matched_scope = None
        else:
            matched_scope = next(
                (scope for scope in principal.scopes if scope.covers(requested_scope)),
                None,
            )
            if matched_scope is None:
                reason = DenialReason.OUT_OF_SCOPE
            else:
                return PolicyDecision(
                    allowed=True,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    policy_version=self.policy_version,
                    decision_id=decision_id,
                    matched_scope=matched_scope,
                )
        return PolicyDecision(
            allowed=False,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            policy_version=self.policy_version,
            decision_id=decision_id,
            matched_scope=matched_scope,
            reason=reason,
        )


__all__ = ["LocalPolicyDecisionPoint", "PolicyDecision", "PolicyDecisionPoint"]
