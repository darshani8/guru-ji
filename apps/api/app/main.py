from __future__ import annotations

import atexit
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.dependencies import build_runtime
from .api.error_handlers import guruji_error_handler
from .api.routes.audit import router as audit_router
from .api.routes.briefings import router as briefings_router
from .api.routes.chat import router as chat_router
from .api.routes.health import router as health_router
from .api.routes.research import router as research_router
from .api.routes.sources import router as sources_router
from .api.routes.voice import router as voice_router
from .config.settings import AppSettings
from .domain.errors import GuruJiError
from .middleware.request_id import RequestIdMiddleware
from .middleware.request_size import RequestSizeLimitMiddleware

settings = AppSettings.from_env()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.runtime.store.close()

app = FastAPI(title=settings.app_name, description="Policy-bound, read-only institutional intelligence", version=settings.version, lifespan=lifespan)
app.state.runtime = build_runtime(settings)
atexit.register(app.state.runtime.store.close)
app.add_exception_handler(GuruJiError, guruji_error_handler)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
if settings.allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_credentials=True, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])
app.include_router(health_router)
app.include_router(chat_router)
app.include_router(briefings_router)
app.include_router(voice_router)
app.include_router(sources_router)
app.include_router(research_router)
app.include_router(audit_router)
web_root = Path(__file__).resolve().parents[2] / "web"
if web_root.exists():
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

__all__ = ["app"]
