"""Convenience checks for institution-scoped policy decisions."""

from .authorization import DenialReason
from ..domain.principals import InstitutionScope, Principal


def scope_denial(principal: Principal, requested_scope: InstitutionScope) -> DenialReason | None:
    if not principal.active:
        return DenialReason.UNAUTHENTICATED
    if not principal.can_access(requested_scope):
        return DenialReason.OUT_OF_SCOPE
    return None


__all__ = ["scope_denial"]
