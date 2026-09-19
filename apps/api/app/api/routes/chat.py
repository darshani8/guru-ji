"""Text chat route."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import InstitutionScope
from ...domain.requests import ChatRequest, InteractionChannel


class ScopeBody(BaseModel):
    college_id: str = "college_a"
    department_id: str | None = None
    batch_id: str | None = None


class ChatBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=12_000)
    request_id: str | None = None
    conversation_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    channel: str = "text"
    institution_scope: ScopeBody = Field(default_factory=ScopeBody)


router = APIRouter(prefix="/v1/chat", tags=["chat"])


@router.post("")
async def chat(body: ChatBody, request: Request) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    try:
        channel = InteractionChannel(body.channel)
        request_id = body.request_id or request.headers.get("x-request-id") or f"req-{uuid4().hex}"
        domain_request = ChatRequest(
            request_id=request_id, principal_id=principal.principal_id, prompt=body.prompt,
            institution_scope=InstitutionScope(
                college_id=body.institution_scope.college_id,
                department_id=body.institution_scope.department_id, batch_id=body.institution_scope.batch_id,
            ), source_ids=tuple(body.source_ids), conversation_id=body.conversation_id, channel=channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    answer = await runtime.assistant.ask(domain_request, principal)
    return answer.as_dict()


__all__ = ["router"]
