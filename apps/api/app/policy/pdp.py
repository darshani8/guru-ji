"""Agentic Saffron policy decision point boundaries.

The local PDP is useful for tests and explicit development runs. Production can
replace it with the Cerbos adapter in this module without changing orchestration
or connector contracts. Every implementation is fail-closed.
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


def _denied(
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    policy_version: str,
    decision_id: str,
    reason: DenialReason | str,
    matched_scope: InstitutionScope | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        policy_version=policy_version,
        decision_id=decision_id,
        matched_scope=matched_scope,
        reason=reason,
    )


class LocalPolicyDecisionPoint:
    """Small fail-closed PDP used for development and isolated tests."""

    policy_version = "guru-local-v1"
    _allowed_actions = frozenset({"list", "search", "retrieve", "mcp.tool.call", "execute", "write", "high_risk"})

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
            return _denied(
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                policy_version=self.policy_version,
                decision_id=decision_id,
                reason=DenialReason.UNKNOWN_ACTION,
            )
        if not principal.authenticated or principal.principal_type is PrincipalType.ANONYMOUS:
            reason: DenialReason | str = DenialReason.UNAUTHENTICATED
            matched_scope = None
        elif principal.revoked:
            reason = DenialReason.REVOKED
            matched_scope = None
        elif not principal.has_capability(required_capability):
            reason = DenialReason.MISSING_CAPABILITY
            matched_scope = None
        elif (
            principal.principal_type is PrincipalType.STUDENT
            and requested_scope.batch_id is not None
            and not principal.consent_verified
        ):
            reason = DenialReason.PARENTAL_CONSENT_REQUIRED
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
        return _denied(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            policy_version=self.policy_version,
            decision_id=decision_id,
            matched_scope=matched_scope,
            reason=reason,
        )


__all__ = ["LocalPolicyDecisionPoint", "PolicyDecision", "PolicyDecisionPoint"]
