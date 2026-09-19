"""Auditable request metadata route."""

from fastapi import APIRouter, HTTPException, Request

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import Capability


router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("/recent")
async def recent_audit(request: Request, limit: int = 50) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated or Capability.MANAGE_ACCESS not in principal.capabilities:
        raise HTTPException(status_code=403, detail="access:manage capability is required")
    events = runtime.store.recent_audit(min(max(limit, 1), 100))
    return {"events": [
        {
            "event_id": event.event_id, "event_type": event.event_type, "request_id": event.request_id,
            "occurred_at": event.occurred_at.isoformat(), "principal_id": event.principal_id,
            "outcome": event.outcome.value, "source_ids": list(event.source_ids),
            "tool_names": list(event.tool_names), "duration_ms": event.duration_ms,
        } for event in events
    ]}


__all__ = ["router"]
