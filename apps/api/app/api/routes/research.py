"""Explicit public-web research route with allowlist and audit boundaries."""

from __future__ import annotations

from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.audit import AuditEvent, AuditOutcome
from ...domain.principals import Capability
from ...web_research.search import WebSearchUnavailable


class WebResearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    allowed_domains: list[str] | None = Field(default=None, max_length=20)
    max_results: int = Field(default=5, ge=1, le=10)


router = APIRouter(prefix="/v1/research", tags=["research"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req-{uuid4().hex}"


def _audit(
    request: Request,
    *,
    principal_id: str | None,
    request_id: str,
    outcome: AuditOutcome,
    duration_ms: int,
) -> None:
    runtime = runtime_from_request(request)
    runtime.store.append_audit(AuditEvent(
        event_id=f"audit-{uuid4().hex}",
        event_type="web.research",
        request_id=request_id,
        principal_id=principal_id,
        endpoint="/v1/research/web",
        source_ids=("public_web",),
        tool_names=("web.search", "web.extract"),
        outcome=outcome,
        redactions_applied=("provider_credentials", "raw_prompt_not_persisted"),
        duration_ms=max(0, duration_ms),
    ))


def _status(report: dict[str, object]) -> str:
    results = report.get("results", [])
    if not isinstance(results, list) or not results:
        return "partial"
    return "complete" if all(
        isinstance(item, dict) and item.get("extracted") is True
        for item in results
    ) else "partial"


def _require_research_principal(request: Request, request_id: str):
    principal = principal_from_request(request)
    if not principal.active:
        _audit(request, principal_id=None, request_id=request_id, outcome=AuditOutcome.DENIED, duration_ms=0)
        raise HTTPException(status_code=401, detail="authentication is required")
    if not principal.has_capability(Capability.ASK_READ_ONLY):
        _audit(
            request,
            principal_id=principal.principal_id,
            request_id=request_id,
            outcome=AuditOutcome.DENIED,
            duration_ms=0,
        )
        raise HTTPException(status_code=403, detail="ask:read_only capability is required")
    return principal


@router.post("/web")
async def web_research(body: WebResearchBody, request: Request) -> dict[str, object]:
    request_id = _request_id(request)
    principal = _require_research_principal(request, request_id)
    runtime = runtime_from_request(request)
    if runtime.web_research is None:
        _audit(
            request,
            principal_id=principal.principal_id,
            request_id=request_id,
            outcome=AuditOutcome.FAILED,
            duration_ms=0,
        )
        raise HTTPException(status_code=503, detail="public-web research is not configured")

    started = monotonic()
    try:
        report = await runtime.web_research.search(
            body.query,
            allowed_domains=body.allowed_domains,
            max_results=body.max_results,
        )
    except ValueError as exc:
        _audit(
            request,
            principal_id=principal.principal_id,
            request_id=request_id,
            outcome=AuditOutcome.DENIED,
            duration_ms=int((monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WebSearchUnavailable as exc:
        _audit(
            request,
            principal_id=principal.principal_id,
            request_id=request_id,
            outcome=AuditOutcome.FAILED,
            duration_ms=int((monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = report.as_dict()
    status = _status(payload)
    _audit(
        request,
        principal_id=principal.principal_id,
        request_id=request_id,
        outcome=AuditOutcome.SUCCESS if status == "complete" else AuditOutcome.PARTIAL,
        duration_ms=int((monotonic() - started) * 1000),
    )
    return {"request_id": request_id, "status": status, **payload}


__all__ = ["router"]
