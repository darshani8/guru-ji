from __future__ import annotations

from datetime import date
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import InstitutionScope
from ...domain.requests import ChatRequest, InteractionChannel


class BriefingBody(BaseModel):
    report_date: date | None = None
    college_id: str = "college_a"
    include_web_context: bool = False


router = APIRouter(prefix="/v1/briefings", tags=["briefings"])


@router.post("/daily")
async def daily_briefing(body: BriefingBody, request: Request) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    if body.include_web_context:
        raise HTTPException(status_code=501, detail="public-web context is not enabled in the local reference build")

    request_id = f"brief-{uuid4().hex}"
    domain_request = ChatRequest(
        request_id=request_id,
        principal_id=principal.principal_id,
        prompt="Prepare a daily institutional overview",
        institution_scope=InstitutionScope(body.college_id),
        source_ids=(),
        conversation_id=None,
        channel=InteractionChannel.TEXT,
    )
    answer = await runtime.assistant.ask(domain_request, principal)
    payload: dict[str, object] = {
        "briefing_id": request_id,
        "briefing_type": "daily_institutional",
        "report_date": (body.report_date or date.today()).isoformat(),
        "answer": answer.as_dict(),
        "web_context_included": False,
    }
    runtime.store.record_briefing(request_id, principal.principal_id, body.college_id, payload)
    return payload


@router.get("/recent")
async def recent_briefings(request: Request, limit: int = 20) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return {"briefings": list(runtime.store.recent_briefings(limit))}


__all__ = ["router"]
