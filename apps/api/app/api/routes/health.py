from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/v1/health", tags=["health"])


@router.get("/live", summary="Check whether the API process is running")
async def liveness(request: Request) -> dict[str, str]:
    settings = request.app.state.runtime.settings
    return {"service": settings.app_name, "status": "ok", "version": settings.version}


async def _ping(store: object, name: str) -> bool:
    """A store that cannot be reached reports not ready instead of a 500."""

    try:
        return bool(await run_in_threadpool(store.ping))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the probe must answer whatever the driver raised
        logger.warning("readiness probe: %s store is unreachable", name, exc_info=True)
        return False


@router.get("/ready", summary="Check whether the local runtime is ready", responses={503: {"description": "A store is unreachable"}})
async def readiness(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    # Pings take the store lock; run them off the event loop so a long worker
    # transaction delays this probe instead of stalling every other request.
    database_ok = await _ping(runtime.store, "control")
    platform = runtime.platform
    platform_ok = await _ping(platform.store, "institution") if platform is not None else True
    status = "ready" if database_ok and platform_ok else "not_ready"
    payload: dict[str, object] = {"service": runtime.settings.app_name, "status": status, "version": runtime.settings.version, "sources": len(runtime.sources.all()), "tools": len(runtime.tools.all()), "database": runtime.store.backend_name, "database_ok": database_ok}
    payload["platform"] = (
        {
            "enabled": True, "institution_database": platform.store.backend_name, "institution_database_ok": platform_ok, "object_store": platform.objects.backend_name,
            "job_queue": platform.jobs.backend_name, "platform_tools": len(platform.registry.all()), "internet_intelligence": platform.intelligence is not None,
            "ocr": platform.parsers.ocr_engine.engine_name, "embeddings": platform.documents.embeddings.provider_name,
        }
        if platform is not None else {"enabled": False}
    )
    if status != "ready":
        return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
    return payload


__all__ = ["router"]
