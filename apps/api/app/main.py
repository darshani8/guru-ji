"""Guru Ji's HTTP application boundary."""

from fastapi import FastAPI

from app.api.routes.health import router as health_router

APP_DESCRIPTION = (
    "A federated, read-only institutional AI assistant with text and voice channels."
)
APP_VERSION = "0.1.0"

app = FastAPI(
    title="Guru Ji API",
    description=APP_DESCRIPTION,
    version=APP_VERSION,
)

app.include_router(health_router)


__all__ = ["app"]
