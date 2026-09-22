"""Pure, deny-by-default authorization decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType


class DenialReason(StrEnum):
    """Stable reasons that can be logged without exposing sensitive data."""

    UNAUTHENTICATED = "unauthenticated"
    REVOKED = "revoked"
    MISSING_CAPABILITY = "missing_capability"
    OUT_OF_SCOPE = "out_of_scope"
    PARENTAL_CONSENT_REQUIRED = "parental_consent_required"
    UNKNOWN_ACTION = "unknown_action"
    PDP_UNAVAILABLE = "pdp_unavailable"
    STALE_POLICY = "stale_policy"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """The only result consumed by orchestration before work is planned."""

    allowed: bool
    reason: DenialReason | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("allowed decisions cannot contain a denial reason")
        if not self.allowed and self.reason is None:
            raise ValueError("denied decisions require a denial reason")

    @classmethod
    def allow(cls) -> "AuthorizationDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: DenialReason) -> "AuthorizationDecision":
        return cls(allowed=False, reason=reason)


def authorize(
    principal: Principal,
    required_capability: Capability,
    requested_scope: InstitutionScope,
) -> AuthorizationDecision:
    """Evaluate identity, capability, consent, and scope without mutation."""

    if not principal.authenticated or principal.principal_type is PrincipalType.ANONYMOUS:
        return AuthorizationDecision.deny(DenialReason.UNAUTHENTICATED)
    if principal.revoked:
        return AuthorizationDecision.deny(DenialReason.REVOKED)
    if not principal.has_capability(required_capability):
        return AuthorizationDecision.deny(DenialReason.MISSING_CAPABILITY)
    if (
        principal.principal_type is PrincipalType.STUDENT
        and requested_scope.batch_id is not None
        and not principal.consent_verified
    ):
        return AuthorizationDecision.deny(DenialReason.PARENTAL_CONSENT_REQUIRED)
    if not principal.can_access(requested_scope):
        return AuthorizationDecision.deny(DenialReason.OUT_OF_SCOPE)

    return AuthorizationDecision.allow()


__all__ = ["AuthorizationDecision", "DenialReason", "authorize"]
