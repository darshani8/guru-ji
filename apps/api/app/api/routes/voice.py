"""Bounded browser voice session and transcript transport routes."""

from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import InstitutionScope, Principal
from ...domain.requests import ChatRequest, InteractionChannel
from ...voice.protocol import (
    AuthenticateMessage,
    CloseMessage,
    PingMessage,
    UtteranceMessage,
    VOICE_MESSAGE_ADAPTER,
)
from ...voice.realtime_events import RealtimeEvent
from ...voice.session_manager import VoiceScopeError


router = APIRouter(prefix="/v1/voice", tags=["voice"])
MAX_EVENT_BYTES = 32_000
MAX_UTTERANCES_PER_MINUTE = 20
AUTH_TIMEOUT_SECONDS = 10.0


class VoiceSessionBody(BaseModel):
    college_id: str | None = Field(default=None, min_length=1, max_length=128)
    department_id: str | None = Field(default=None, min_length=1, max_length=128)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)


def _requested_scope(body: VoiceSessionBody | None, principal: Principal) -> InstitutionScope:
    if body is None or body.college_id is None:
        if not principal.scopes:
            raise HTTPException(status_code=403, detail="an institution scope is required")
        return principal.scopes[0]
    return InstitutionScope(
        college_id=body.college_id,
        department_id=body.department_id,
        batch_id=body.batch_id,
    )


def _websocket_url(request: Request, session_id: str) -> str:
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return f"{scheme}://{request.url.netloc}/v1/voice/sessions/{session_id}/stream"


def _origin_allowed(websocket: WebSocket, environment: str, allowed_origins: tuple[str, ...]) -> bool:
    origin = websocket.headers.get("origin")
    if environment in {"development", "test"}:
        if not origin:
            return True
        if origin in allowed_origins or origin == "http://testserver":
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not origin or origin not in allowed_origins:
        return False
    return urlparse(origin).scheme == "https"


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        # The peer may have closed before the server could send the close frame.
        pass


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json({"type": RealtimeEvent.ERROR, "code": code, "message": message})


@router.post("/sessions")
async def create_session(
    request: Request,
    body: VoiceSessionBody | None = None,
) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.active:
        raise HTTPException(status_code=401, detail="authentication is required")
    scope = _requested_scope(body, principal)
    try:
        session, transport_ticket = runtime.voice.create_access(principal, scope)
    except VoiceScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "session_id": session.session_id,
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "status": session.status,
        "audio_retention": session.audio_retention,
        "transport": "browser_web_speech_ws",
        "transport_ticket": transport_ticket,
        "websocket_url": _websocket_url(request, session.session_id),
        "server_receives": "final_transcripts_only",
    }


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str, request: Request) -> dict[str, bool]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.active:
        raise HTTPException(status_code=401, detail="authentication is required")
    return {"closed": runtime.voice.close(session_id, principal.principal_id)}


@router.websocket("/sessions/{session_id}/stream")
async def voice_stream(session_id: str, websocket: WebSocket) -> None:
    runtime = websocket.app.state.runtime
    if not _origin_allowed(websocket, runtime.settings.environment, runtime.settings.allowed_origins):
        await _close(websocket, 1008, "origin_not_allowed")
        return

    await websocket.accept()
    principal = None
    try:
        try:
            first = await asyncio.wait_for(websocket.receive(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _close(websocket, 4401, "authentication_timeout")
            return
        if first.get("type") == "websocket.disconnect":
            return
        if first.get("bytes") is not None or first.get("text") is None:
            await _close(websocket, 1003, "text_messages_only")
            return
        try:
            auth_message = VOICE_MESSAGE_ADAPTER.validate_json(first["text"])
        except ValidationError:
            await _close(websocket, 4401, "invalid_auth_message")
            return
        if not isinstance(auth_message, AuthenticateMessage):
            await _close(websocket, 4401, "authentication_required")
            return
        principal = runtime.voice.claim_transport(session_id, auth_message.ticket)
        session = runtime.voice.get(session_id)
        if principal is None or session is None:
            await _close(websocket, 4401, "invalid_or_expired_ticket")
            return

        await websocket.send_json({
            "type": RealtimeEvent.READY,
            "session_id": session.session_id,
            "expires_at": session.expires_at.isoformat(),
            "transport": "browser_web_speech_ws",
            "server_receives": "final_transcripts_only",
        })

        utterance_times: deque[float] = deque()
        while True:
            session = runtime.voice.get(session_id)
            if session is None:
                await websocket.send_json({"type": RealtimeEvent.EXPIRED})
                await _close(websocket, 4001, "session_expired")
                return

            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await _send_error(websocket, "binary_audio_not_supported", "Send final text transcripts only.")
                await _close(websocket, 1003, "binary_audio_not_supported")
                return
            raw_text = message.get("text")
            if raw_text is None or len(raw_text.encode("utf-8")) > MAX_EVENT_BYTES:
                await _send_error(websocket, "message_too_large", "Voice event exceeds the transport limit.")
                await _close(websocket, 1009, "message_too_large")
                return
            try:
                event = VOICE_MESSAGE_ADAPTER.validate_json(raw_text)
            except ValidationError:
                await _send_error(websocket, "invalid_event", "Voice events must match the supported JSON schema.")
                continue

            if isinstance(event, PingMessage):
                await websocket.send_json({"type": RealtimeEvent.PONG})
                continue
            if isinstance(event, CloseMessage):
                await websocket.send_json({"type": RealtimeEvent.SESSION_CLOSED, "session_id": session_id})
                await _close(websocket, 1000, "client_closed")
                return
            if not isinstance(event, UtteranceMessage):
                await _send_error(websocket, "authentication_not_allowed", "Authentication is only valid as the first event.")
                continue

            now = monotonic()
            while utterance_times and now - utterance_times[0] >= 60:
                utterance_times.popleft()
            if len(utterance_times) >= MAX_UTTERANCES_PER_MINUTE:
                await _send_error(websocket, "rate_limited", "Voice utterance limit reached for this session.")
                continue
            utterance_times.append(now)

            text = event.text.strip()
            conversation_id = event.conversation_id
            client_message_id = event.client_message_id
            request_id = f"voice-{uuid4().hex}"
            chat_request = ChatRequest(
                request_id=request_id,
                principal_id=principal.principal_id,
                prompt=text,
                institution_scope=session.institution_scope,
                conversation_id=conversation_id.strip() if conversation_id else None,
                channel=InteractionChannel.VOICE,
            )
            answer = await runtime.assistant.ask(chat_request, principal)
            await websocket.send_json({
                "type": RealtimeEvent.ANSWER,
                "client_message_id": client_message_id,
                "answer": answer.as_dict(),
            })
    except WebSocketDisconnect:
        return
    finally:
        runtime.voice.release_transport(session_id)


__all__ = ["router"]
