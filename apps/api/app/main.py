from __future__ import annotations

import atexit
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.dependencies import build_runtime
from .api.error_handlers import guruji_error_handler, http_exception_handler, validation_exception_handler
from .api.routes.agent import router as agent_router
from .api.routes.audit import router as audit_router
from .api.routes.auth import router as auth_router
from .api.routes.briefings import router as briefings_router
from .api.routes.chat import router as chat_router
from .api.routes.data import router as data_router
from .api.routes.documents import router as documents_router
from .api.routes.health import router as health_router
from .api.routes.ingestion import router as ingestion_router
from .api.routes.intelligence import router as intelligence_router
from .api.routes.platform import router as platform_router
from .api.routes.research import router as research_router
from .api.routes.sources import router as sources_router
from .api.routes.voice import router as voice_router
from .config.settings import AppSettings
from .domain.errors import GuruJiError
from .middleware.rate_limit import RateLimitMiddleware
from .middleware.request_id import RequestIdMiddleware
from .middleware.request_size import RequestSizeLimitMiddleware
from .middleware.security_headers import SecurityHeadersMiddleware
from .middleware.timeout import RequestTimeoutMiddleware

settings = AppSettings.from_env()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.runtime.close()

app = FastAPI(title=settings.app_name, description="AI-powered institutional intelligence platform: ingestion, canonical data, policy-bound agents, and internet intelligence", version=settings.version, lifespan=lifespan)
app.state.runtime = build_runtime(settings)
atexit.register(app.state.runtime.close)
app.add_exception_handler(GuruJiError, guruji_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware, production=settings.environment == "production", auth_origins=settings.oidc_browser_origins)
app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=settings.request_timeout_seconds)
app.add_middleware(RateLimitMiddleware, max_requests=settings.rate_limit_requests, window_seconds=settings.rate_limit_window_seconds)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes, upload_max_bytes=settings.max_upload_bytes + 65_536, upload_prefixes=("/v1/ingestion/uploads", "/v1/documents"))
if settings.allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_credentials=True, allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"])
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(briefings_router)
app.include_router(voice_router)
app.include_router(sources_router)
app.include_router(research_router)
app.include_router(audit_router)
app.include_router(ingestion_router)
app.include_router(data_router)
app.include_router(agent_router)
app.include_router(documents_router)
app.include_router(intelligence_router)
app.include_router(platform_router)
web_root = Path(__file__).resolve().parents[2] / "web"
if web_root.exists():
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

__all__ = ["app"]
