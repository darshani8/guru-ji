from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool


router = APIRouter(prefix="/v1/health", tags=["health"])


@router.get("/live", summary="Check whether the API process is running")
async def liveness(request: Request) -> dict[str, str]:
    settings = request.app.state.runtime.settings
    return {"service": settings.app_name, "status": "ok", "version": settings.version}


@router.get("/ready", summary="Check whether the local runtime is ready")
async def readiness(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    # Pings take the store lock; run them off the event loop so a long worker
    # transaction delays this probe instead of stalling every other request.
    database_ok = await run_in_threadpool(runtime.store.ping)
    platform = runtime.platform
    platform_ok = await run_in_threadpool(platform.store.ping) if platform is not None else True
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
    return payload


__all__ = ["router"]
