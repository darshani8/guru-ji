from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter(prefix="/v1/health", tags=["health"])


@router.get("/live", summary="Check whether the API process is running")
async def liveness(request: Request) -> dict[str, str]:
    settings = request.app.state.runtime.settings
    return {"service": settings.app_name, "status": "ok", "version": settings.version}


@router.get("/ready", summary="Check whether the local runtime is ready")
async def readiness(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    database_ok = runtime.store.ping()
    status = "ready" if database_ok else "not_ready"
    return {"service": runtime.settings.app_name, "status": status, "version": runtime.settings.version, "sources": len(runtime.sources.all()), "tools": len(runtime.tools.all()), "database": runtime.store.backend_name, "database_ok": database_ok}


__all__ = ["router"]
