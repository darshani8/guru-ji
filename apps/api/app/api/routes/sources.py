"""Approved source metadata and health route."""

from fastapi import APIRouter, HTTPException, Request

from ..dependencies import principal_from_request, runtime_from_request
from ...domain.principals import Capability


router = APIRouter(prefix="/v1/sources", tags=["sources"])


@router.get("")
async def list_sources(request: Request) -> dict[str, object]:
    runtime = runtime_from_request(request)
    principal = principal_from_request(request)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="authentication is required")
    if Capability.VIEW_SOURCE_METADATA not in principal.capabilities:
        raise HTTPException(status_code=403, detail="source:view_metadata capability is required")
    items = []
    for definition in runtime.sources.all():
        health = await runtime.connectors.get(definition.source_id).health()
        runtime.store.set_health(health)
        items.append({
            "source_id": definition.source_id, "institution_id": definition.institution_id,
            "display_name": definition.display_name, "connector_type": definition.connector_type,
            "status": definition.status.value, "health": health.status.value,
            "freshness": health.freshness.value, "detail": health.detail,
        })
    return {"sources": items}


__all__ = ["router"]
