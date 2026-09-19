"""Context passed into connectors after policy checks."""

from dataclasses import dataclass

from ..domain.principals import InstitutionScope, Principal
from ..policy.query_limits import QueryLimits


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request_id: str
    principal: Principal
    institution_scope: InstitutionScope
    limits: QueryLimits


__all__ = ["ExecutionContext"]
