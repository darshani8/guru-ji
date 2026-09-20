from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..auth.oidc import JwtVerifier
from ..auth.principal import principal_from_headers
from ..config.settings import AppSettings
from ..config.source_registry import SourceDefinition, SourceRegistry
from ..integrations.edge import EdgeIdentityAdapter, MoodleAdapter, OpenEdxAdapter
from ..connectors.college_a.connector import CollegeADemoConnector
from ..connectors.registry import ConnectorRegistry
from ..connectors.remote_http import RemoteHttpConnector
from ..connectors.base import ReadOnlyConnector
from ..observability.export import HttpJsonTraceExporter
from ..observability.tracing import TraceRecorder
from ..orchestration.assistant_service import AssistantService
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.cerbos import CerbosPolicyDecisionPoint
from ..policy.pdp import LocalPolicyDecisionPoint, PolicyDecisionPoint
from ..policy.query_limits import QueryLimits
from ..providers.litellm import LiteLLMProvider
from ..providers.ollama import OllamaProvider
from ..tools.college_tools import build_college_tools
from ..tools.health_tools import build_health_tools
from ..tools.registry import ToolRegistry
from ..voice.session_manager import VoiceSessionManager
from ..web_research.extractor import AllowlistedHttpExtractor
from ..web_research.research_service import PublicWebResearchService
from ..web_research.search import TavilyHttpSearchProvider


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
    pdp: PolicyDecisionPoint
    tracer: TraceRecorder
    web_research: PublicWebResearchService | None = None
    auth_verifier: JwtVerifier | None = None
    edge_identity: EdgeIdentityAdapter | None = None


def _build_sources(settings: AppSettings) -> SourceRegistry:
    definitions: list[SourceDefinition] = []
    if settings.demo_data_enabled:
        definitions.append(SourceDefinition(
            source_id="college_a_demo",
            institution_id="college_a",
            display_name="College A demo source",
            connector_type="deterministic_demo",
            allowed_tools=(
                "institution.overview",
                "institution.attendance_summary",
                "institution.source_health",
            ),
        ))
    if settings.institution_connector_base_url:
        if any(item.source_id == settings.institution_connector_source_id for item in definitions):
            raise ValueError("institution connector source ID conflicts with an existing source")
        definitions.append(SourceDefinition(
            source_id=settings.institution_connector_source_id,
            institution_id=settings.institution_connector_institution_id,
            display_name=settings.institution_connector_display_name,
            connector_type="remote_http",
            allowed_tools=(
                "institution.overview",
                "institution.attendance_summary",
                "institution.source_health",
            ),
        ))
    return SourceRegistry(tuple(definitions))


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
    if settings.model_provider == "litellm":
        return LiteLLMProvider(
            model_id=settings.litellm_model_id,
            api_base=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return None


def _build_pdp(settings: AppSettings) -> PolicyDecisionPoint:
    if settings.pdp_mode == "cerbos":
        return CerbosPolicyDecisionPoint(
            base_url=settings.cerbos_url or "",
            policy_version=settings.cerbos_policy_version,
            timeout_seconds=settings.cerbos_timeout_seconds,
            require_fresh=settings.cerbos_require_fresh,
        )
    return LocalPolicyDecisionPoint()


def _build_edge_identity(settings: AppSettings) -> EdgeIdentityAdapter | None:
    if settings.edge_adapter == "disabled":
        return None
    if not settings.edge_adapter_base_url or not settings.edge_adapter_auth_token:
        raise ValueError("configured edge adapter requires a base URL and server auth token")
    adapter_type = OpenEdxAdapter if settings.edge_adapter == "openedx" else MoodleAdapter
    return adapter_type(
        base_url=settings.edge_adapter_base_url,
        auth_token=settings.edge_adapter_auth_token,
    )


def _build_web_research(settings: AppSettings) -> PublicWebResearchService | None:
    if settings.web_search_provider == "disabled":
        return None
    if settings.web_search_provider != "tavily":
        raise ValueError("unsupported web search provider")
    provider = TavilyHttpSearchProvider(
        api_key=settings.web_search_api_key or "",
        endpoint=settings.web_search_endpoint,
        timeout_seconds=settings.web_search_timeout_seconds,
    )
    extractor = AllowlistedHttpExtractor(
        allowed_domains=frozenset(settings.web_allowed_domains),
        timeout_seconds=settings.web_extract_timeout_seconds,
        max_response_bytes=settings.web_extract_max_bytes,
    )
    return PublicWebResearchService(
        provider=provider,
        extractor=extractor,
        configured_domains=frozenset(settings.web_allowed_domains),
        max_results=settings.web_search_max_results,
    )


def _build_connectors(settings: AppSettings) -> ConnectorRegistry:
    connectors: list[ReadOnlyConnector] = []
    source_ids: list[str] = []
    if settings.demo_data_enabled:
        connectors.append(CollegeADemoConnector())
        source_ids.append("college_a_demo")
    if settings.institution_connector_base_url:
        if settings.institution_connector_source_id in source_ids:
            raise ValueError("institution connector source ID conflicts with an existing connector")
        connectors.append(RemoteHttpConnector(
            source_id=settings.institution_connector_source_id,
            institution_id=settings.institution_connector_institution_id,
            display_name=settings.institution_connector_display_name,
            base_url=settings.institution_connector_base_url,
            allowed_tools=frozenset({
                "institution.overview",
                "institution.attendance_summary",
                "institution.source_health",
            }),
            timeout_seconds=settings.connector_timeout_seconds,
            max_response_bytes=settings.connector_max_response_bytes,
            auth_token=settings.institution_connector_auth_token,
            scope_attestation_required=settings.connector_scope_attestation_required,
        ))
    return ConnectorRegistry(tuple(connectors))


def build_runtime(settings: AppSettings | None = None) -> Runtime:
    settings = settings or AppSettings.from_env()
    settings.ensure_safe_for_production()
    sources = _build_sources(settings)
    source_ids = tuple(item.source_id for item in sources.all())
    tools = ToolRegistry((*build_college_tools(source_ids), *build_health_tools(source_ids)))
    connectors = _build_connectors(settings)
    store = _build_store(settings)
    store.prune_retention(settings.audit_retention_days)
    pdp = _build_pdp(settings)
    exporter = (
        HttpJsonTraceExporter(settings.otel_exporter_endpoint, settings.otel_exporter_timeout_seconds)
        if settings.otel_exporter_endpoint else None
    )
    tracer = TraceRecorder(exporter=exporter)
    auth_verifier = None if settings.environment in {"development", "test"} else JwtVerifier(settings)
    edge_identity = _build_edge_identity(settings)
    model = _build_model(settings)
    web_research = _build_web_research(settings)
    assistant = AssistantService(
        sources=sources,
        tools=tools,
        connectors=connectors,
        store=store,
        limits=QueryLimits(),
        model=model,
        model_max_tokens=settings.model_max_tokens,
        web_research=web_research,
        pdp=pdp,
        tracer=tracer,
    )
    return Runtime(
        settings=settings,
        sources=sources,
        tools=tools,
        connectors=connectors,
        store=store,
        assistant=assistant,
        voice=VoiceSessionManager(ttl_seconds=300, max_active=10),
        pdp=pdp,
        tracer=tracer,
        web_research=web_research,
        auth_verifier=auth_verifier,
        edge_identity=edge_identity,
    )


def runtime_from_request(request: Request) -> Runtime:
    return request.app.state.runtime


def principal_from_request(request: Request):
    runtime = runtime_from_request(request)
    if runtime.edge_identity is not None:
        try:
            assertion = request.headers.get("x-guru-edge-session", "")
            return runtime.edge_identity.resolve(assertion)
        except (PermissionError, ValueError, RuntimeError):
            return principal_from_headers({}, runtime.settings, runtime.auth_verifier)
    return principal_from_headers(request.headers, runtime.settings, runtime.auth_verifier)


__all__ = ["Runtime", "build_runtime", "principal_from_request", "runtime_from_request"]
