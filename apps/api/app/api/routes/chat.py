"""Text chat routes, including a channel-compatible SSE response."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import InstitutionScope
from ...domain.streaming import (
    AnswerEvent,
    CitationEvent,
    DeltaEvent,
    DoneEvent,
    MessageEndEvent,
    MessageStartEvent,
    StreamSequenceValidator,
    WarningEvent,
    to_sse,
)
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


def _build_request(body: ChatBody, request: Request, principal) -> ChatRequest:
    try:
        channel = InteractionChannel(body.channel)
        request_id = body.request_id or request.headers.get("x-request-id") or f"req-{uuid4().hex}"
        return ChatRequest(
            request_id=request_id,
            principal_id=principal.principal_id,
            prompt=body.prompt,
            institution_scope=InstitutionScope(
                college_id=body.institution_scope.college_id,
                department_id=body.institution_scope.department_id,
                batch_id=body.institution_scope.batch_id,
            ),
            source_ids=tuple(body.source_ids),
            conversation_id=body.conversation_id,
            channel=channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_principal(request: Request):
    principal = principal_from_request(request)
    if not principal.active:
        raise HTTPException(status_code=401, detail="authentication is required")
    return principal


async def _stream_answer(body: ChatBody, request: Request, principal) -> StreamingResponse:
    runtime = runtime_from_request(request)
    domain_request = _build_request(body, request, principal)
    answer = await runtime.assistant.ask(domain_request, principal)
    answer_payload = answer.as_dict()
    status: Literal["complete", "partial", "refused", "failed", "degraded"] = (
        answer.status if answer.status in {"complete", "partial", "refused", "failed", "degraded"} else "failed"
    )

    async def events():
        validator = StreamSequenceValidator()
        sequence = 0
        chunks: list[str] = []

        def emit(event):
            nonlocal sequence
            sequence += 1
            return to_sse(validator.accept(event))

        chunks.append(emit(MessageStartEvent(
            request_id=answer.request_id,
            conversation_id=body.conversation_id,
            sequence=sequence + 1,
        )))
        for citation in answer.citations:
            chunks.append(emit(CitationEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=sequence + 1,
                citation=dict(citation),
            )))
        for warning in answer.warnings:
            chunks.append(emit(WarningEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=sequence + 1,
                warning=dict(warning),
            )))
        if answer.answer:
            chunks.append(emit(DeltaEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=sequence + 1,
                text=answer.answer,
            )))
        chunks.append(emit(AnswerEvent(
            request_id=answer.request_id,
            conversation_id=body.conversation_id,
            sequence=sequence + 1,
            answer=answer_payload,
        )))
        chunks.append(emit(MessageEndEvent(
            request_id=answer.request_id,
            conversation_id=body.conversation_id,
            sequence=sequence + 1,
            status=status,
        )))
        chunks.append(emit(DoneEvent(
            request_id=answer.request_id,
            conversation_id=body.conversation_id,
            sequence=sequence + 1,
        )))
        yield "".join(chunks)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("", response_model=None)
async def chat(body: ChatBody, request: Request, stream: bool = False) -> dict[str, object] | StreamingResponse:
    principal = _require_principal(request)
    accepts_stream = "text/event-stream" in request.headers.get("accept", "").lower()
    if stream or accepts_stream:
        return await _stream_answer(body, request, principal)
    runtime = runtime_from_request(request)
    answer = await runtime.assistant.ask(_build_request(body, request, principal), principal)
    return answer.as_dict()


@router.post("/stream")
async def chat_stream(body: ChatBody, request: Request) -> StreamingResponse:
    """Return the policy-bound answer through a validated SSE event sequence."""

    return await _stream_answer(body, request, _require_principal(request))


__all__ = ["router"]
