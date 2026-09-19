from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..auth.oidc import JwtVerifier
from ..auth.principal import principal_from_headers
from ..config.settings import AppSettings
from ..config.source_registry import SourceDefinition, SourceRegistry
from ..connectors.college_a.connector import CollegeADemoConnector
from ..connectors.registry import ConnectorRegistry
from ..orchestration.assistant_service import AssistantService
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.query_limits import QueryLimits
from ..providers.ollama import OllamaProvider
from ..tools.college_tools import COLLEGE_TOOLS
from ..tools.health_tools import HEALTH_TOOLS
from ..tools.registry import ToolRegistry
from ..voice.session_manager import VoiceSessionManager


ControlStore = InMemoryControlStore | PostgresControlStore | SqliteControlStore


@dataclass(slots=True)
class Runtime:
    settings: AppSettings
    sources: SourceRegistry
    tools: ToolRegistry
    connectors: ConnectorRegistry
    store: ControlStore
    assistant: AssistantService
    voice: VoiceSessionManager
    auth_verifier: JwtVerifier | None = None


def _build_sources() -> SourceRegistry:
    return SourceRegistry((
        SourceDefinition(
            source_id="college_a_demo",
            institution_id="college_a",
            display_name="College A demo source",
            connector_type="deterministic_demo",
            allowed_tools=(
                "institution.overview",
                "institution.attendance_summary",
                "institution.source_health",
            ),
        ),
    ))


def _build_store(settings: AppSettings) -> ControlStore:
    database_url = settings.control_database_url
    if not database_url:
        return InMemoryControlStore()
    if database_url.startswith("sqlite://") or database_url == ":memory:":
        return SqliteControlStore(database_url)
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresControlStore(database_url)
    raise ValueError("CONTROL_DATABASE_URL must use sqlite://, postgresql://, or postgres://")


def _build_model(settings: AppSettings):
    if settings.model_provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model_id=settings.ollama_model_id,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return None


def build_runtime(settings: AppSettings | None = None) -> Runtime:
    settings = settings or AppSettings.from_env()
    settings.ensure_safe_for_production()
    sources = _build_sources()
    tools = ToolRegistry((*COLLEGE_TOOLS, *HEALTH_TOOLS))
    connectors = ConnectorRegistry((CollegeADemoConnector(),))
    store = _build_store(settings)
    auth_verifier = None if settings.environment in {"development", "test"} else JwtVerifier(settings)
    model = _build_model(settings)
    return Runtime(
        settings=settings,
        sources=sources,
        tools=tools,
        connectors=connectors,
        store=store,
        assistant=AssistantService(
            sources=sources,
            tools=tools,
            connectors=connectors,
            store=store,
            limits=QueryLimits(),
            model=model,
            model_max_tokens=settings.model_max_tokens,
        ),
        voice=VoiceSessionManager(ttl_seconds=300, max_active=10),
        auth_verifier=auth_verifier,
    )


def runtime_from_request(request: Request) -> Runtime:
    return request.app.state.runtime


def principal_from_request(request: Request):
    runtime = runtime_from_request(request)
    return principal_from_headers(request.headers, runtime.settings, runtime.auth_verifier)


__all__ = ["Runtime", "build_runtime", "principal_from_request", "runtime_from_request"]
