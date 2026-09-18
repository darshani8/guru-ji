"""Health routes for the Guru Ji API."""

from fastapi import APIRouter

SERVICE_NAME = "guru-ji-api"
SERVICE_VERSION = "0.1.0"

router = APIRouter(prefix="/v1/health", tags=["health"])


@router.get(
    "/live",
    summary="Check whether the API process is running",
)
async def liveness() -> dict[str, str]:
    """Return process-level liveness without checking external dependencies."""

    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "version": SERVICE_VERSION,
    }


__all__ = ["router"]
