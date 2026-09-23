"""Bounded browser voice session and transcript transport routes."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from ..dependencies import principal_from_request, runtime_from_request
from ...conversation.contracts import LANGUAGES
from ...domain.principals import InstitutionScope, Principal
from ...voice.protocol import AuthenticateMessage, VOICE_MESSAGE_ADAPTER
from ...voice.realtime_events import RealtimeEvent
from ...voice.session_manager import VoiceScopeError
from ...voice.stream import MAX_EVENT_BYTES, VoiceConnection


router = APIRouter(prefix="/v1/voice", tags=["voice"])
AUTH_TIMEOUT_SECONDS = 10.0
FEATURES = ("thinking", "speech", "interrupt")


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


def _tts_description(runtime) -> dict[str, object]:
    return runtime.tts.describe()


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
        "ticket_expires_at": session.ticket_expires_at.isoformat() if session.ticket_expires_at else None,
        "status": session.status,
        "audio_retention": session.audio_retention,
        "transport": "browser_web_speech_ws",
        "transport_ticket": transport_ticket,
        "websocket_url": _websocket_url(request, session.session_id),
        "server_receives": "final_transcripts_only",
        "features": list(FEATURES),
        "languages": list(LANGUAGES),
        "tts": _tts_description(runtime),
        "idle_timeout_seconds": runtime.settings.voice_idle_timeout_seconds,
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
    claimed = False
    try:
        try:
            first = await asyncio.wait_for(websocket.receive(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _close(websocket, 4401, "authentication_timeout")
            return
        if first.get("type") == "websocket.disconnect":
            return
        if first.get("bytes") is not None or first.get("text") is None or len(first["text"].encode("utf-8")) > MAX_EVENT_BYTES:
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
        session = runtime.voice.get(session_id) if principal is not None else None
        if principal is None or session is None:
            await _close(websocket, 4401, "invalid_or_expired_ticket")
            return
        claimed = True
        features = frozenset(auth_message.features)
        await websocket.send_json({
            "type": RealtimeEvent.READY,
            "session_id": session.session_id,
            "expires_at": session.expires_at.isoformat(),
            "transport": "browser_web_speech_ws",
            "server_receives": "final_transcripts_only",
            "features": sorted(features),
            "languages": list(LANGUAGES),
            "tts": _tts_description(runtime),
            "idle_timeout_seconds": runtime.settings.voice_idle_timeout_seconds,
        })
        connection = VoiceConnection(
            websocket, runtime, session, principal, features,
            utterances_per_minute=runtime.settings.voice_utterances_per_minute,
            idle_timeout_seconds=runtime.settings.voice_idle_timeout_seconds,
        )
        reason = await connection.run()
        if reason == "expired":
            await _close(websocket, 4001, "session_expired")
        elif reason == "client_closed":
            await _close(websocket, 1000, "client_closed")
        elif reason == "binary_audio_not_supported":
            await _close(websocket, 1003, "binary_audio_not_supported")
        elif reason == "message_too_large":
            await _close(websocket, 1009, "message_too_large")
    except WebSocketDisconnect:
        return
    finally:
        if claimed:
            runtime.voice.release_transport(session_id)


__all__ = ["router"]
