"""Voice session lifecycle routes; media transport is intentionally separate."""

from fastapi import APIRouter, HTTPException, Request

from ..dependencies import principal_from_request, runtime_from_request


router = APIRouter(prefix="/v1/voice", tags=["voice"])


@router.post("/sessions")
async def create_session(request: Request) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    try:
        session = runtime.voice.create(principal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "session_id": session.session_id, "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(), "status": session.status,
        "audio_retention": session.audio_retention,
        "transport": "not_configured",
    }


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str, request: Request) -> dict[str, bool]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    return {"closed": runtime.voice.close(session_id, principal.principal_id)}


__all__ = ["router"]
