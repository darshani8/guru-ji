"""Guru Ji's HTTP application boundary."""

from fastapi import FastAPI

APP_DESCRIPTION = (
    "A federated, read-only institutional AI assistant with text and voice channels."
)
APP_VERSION = "0.1.0"

app = FastAPI(
    title="Guru Ji API",
    description=APP_DESCRIPTION,
    version=APP_VERSION,
)


@app.get(
    "/v1/health/live",
    tags=["health"],
    summary="Check whether the API process is running",
)
async def liveness() -> dict[str, str]:
    """Return a process-level liveness signal without touching dependencies."""

    return {
        "service": "guru-ji-api",
        "status": "ok",
        "version": APP_VERSION,
    }


__all__ = ["app"]
