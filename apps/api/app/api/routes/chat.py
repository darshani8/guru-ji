"""Text chat routes, including a channel-compatible SSE response."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import InstitutionScope
from ...domain.streaming import AnswerEvent, DoneEvent, MessageEndEvent, MessageStartEvent, to_sse
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
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    return principal


async def _stream_answer(body: ChatBody, request: Request, principal) -> StreamingResponse:
    runtime = runtime_from_request(request)
    domain_request = _build_request(body, request, principal)
    answer = await runtime.assistant.ask(domain_request, principal)
    answer_payload = answer.as_dict()
    status = answer.status if answer.status in {"complete", "partial", "refused", "failed", "degraded"} else "failed"

    async def events():
        # Keep the bounded answer envelope in one chunk. A future transport
        # optimization may yield additional answer chunks before the final
        # done event without changing the wire format; authorization, audit,
        # and provenance remain completed before streaming begins.
        yield "".join((
            to_sse(MessageStartEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=1,
            )),
            to_sse(AnswerEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=2,
                answer=answer_payload,
            )),
            to_sse(MessageEndEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=3,
                status=status,
            )),
            to_sse(DoneEvent(
                request_id=answer.request_id,
                conversation_id=body.conversation_id,
                sequence=4,
            )),
        ))

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
    """Return the same policy-bound answer through a stable SSE envelope.

    The current answer envelope is bounded, so it emits one complete answer
    event followed by ``done``. The optional model provider is resolved before
    streaming; future transport chunking must not bypass authorization, audit,
    provenance, or the final response contract.
    """

    return await _stream_answer(body, request, _require_principal(request))


__all__ = ["router"]
