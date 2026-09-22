"""Shared helpers for platform routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException, Request

from ...domain.principals import Capability, InstitutionScope, Principal
from ..dependencies import principal_from_request

MAX_JSON_LIST = 500


def request_id_for(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req-{uuid4().hex}"


def require_principal(request: Request, *capabilities: Capability) -> Principal:
    principal = principal_from_request(request)
    if not principal.active:
        raise HTTPException(status_code=401, detail="authentication is required")
    for capability in capabilities:
        if not principal.has_capability(capability):
            raise HTTPException(status_code=403, detail=f"{capability.value} capability is required")
    return principal


def resolve_institution(principal: Principal, requested: str | None) -> str:
    """The institution a request acts on: explicit, or the caller's first scope."""

    if requested:
        institution_id = requested.strip()
        if not institution_id:
            raise HTTPException(status_code=422, detail="institution_id must not be blank")
    elif principal.scopes:
        institution_id = principal.scopes[0].college_id
    else:
        raise HTTPException(status_code=403, detail="an institution scope is required")
    if not principal.can_access(InstitutionScope(institution_id)):
        raise HTTPException(status_code=403, detail="the requested institution is outside the authenticated scope")
    return institution_id


def translate(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc.args[0]) if exc.args else "not found")
    if isinstance(exc, (ValueError, LookupError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="the request could not be completed")


__all__ = ["MAX_JSON_LIST", "request_id_for", "require_principal", "resolve_institution", "translate"]
