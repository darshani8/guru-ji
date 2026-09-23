from __future__ import annotations

import logging

from dataclasses import dataclass

from fastapi import Request

from ..auth.oidc import JwtVerifier
from ..auth.principal import principal_from_headers
from ..config.settings import AppSettings
from ..config.source_registry import SourceDefinition, SourceRegistry
from ..connectors.base import ReadOnlyConnector
from ..connectors.college_a.connector import CollegeADemoConnector
from ..connectors.registry import ConnectorRegistry
from ..connectors.remote_http import RemoteHttpConnector
from ..integrations.edge import EdgeIdentityAdapter, MoodleAdapter, OpenEdxAdapter
from ..observability.export import HttpJsonTraceExporter
from ..observability.tracing import TraceRecorder
from ..orchestration.assistant_service import AssistantService
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.cerbos import CerbosPolicyDecisionPoint
from ..policy.pdp import LocalPolicyDecisionPoint, PolicyDecisionPoint
from ..policy.query_limits import QueryLimits
from ..providers.anthropic import LATENCY_SENSITIVE_SYSTEM, AnthropicProvider
from ..providers.litellm import LiteLLMProvider
from ..providers.ollama import OllamaProvider
from ..tools.college_tools import build_college_tools
from ..tools.health_tools import build_health_tools
from ..tools.registry import ToolRegistry
from ..voice.session_manager import VoiceSessionManager
from ..web_research.extractor import AllowlistedHttpExtractor
from ..web_research.research_service import PublicWebResearchService
from ..web_research.search import TavilyHttpSearchProvider
from .platform_runtime import PlatformRuntime, build_platform


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
    platform: PlatformRuntime | None = None

    def close(self) -> None:
        if self.platform is not None:
            self.platform.close()
        self.store.close()


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
    for connector in settings.configured_institution_connectors():
        if any(item.source_id == connector.source_id for item in definitions):
            raise ValueError(
                "institution connector source ID conflicts with an existing source: "
                f"{connector.source_id}"
            )
        definitions.append(SourceDefinition(
            source_id=connector.source_id,
            institution_id=connector.institution_id,
            display_name=connector.display_name,
            connector_type="remote_http",
            allowed_tools=connector.allowed_tools,
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
    if settings.model_provider in _CLAUDE_PROVIDERS:
        # Voice and chat answers are what a person waits on: low effort and the
        # latency instruction keep the reply quick.
        return _claude(settings, effort=settings.anthropic_effort, system=LATENCY_SENSITIVE_SYSTEM)
    return None


_CLAUDE_PROVIDERS = frozenset({"anthropic", "bedrock"})


def _claude(settings: AppSettings, *, effort: str, system: str | None = None) -> AnthropicProvider:
    return AnthropicProvider(
        model_id=settings.anthropic_model_id,
        api_key=settings.anthropic_api_key,
        effort=effort,
        system=system,
        timeout_seconds=settings.model_timeout_seconds,
        platform=settings.model_provider,
        aws_region=settings.bedrock_region,
    )


def _build_planner_model(settings: AppSettings, model):
    """The model that plans agent actions; other providers reuse the answer model."""

    if settings.model_provider in _CLAUDE_PROVIDERS:
        return _claude(settings, effort=settings.anthropic_planner_effort)
    return model


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
    source_ids: set[str] = set()
    if settings.demo_data_enabled:
        connectors.append(CollegeADemoConnector())
        source_ids.add("college_a_demo")
    for definition in settings.configured_institution_connectors():
        if definition.source_id in source_ids:
            raise ValueError(
                "institution connector source ID conflicts with an existing connector: "
                f"{definition.source_id}"
            )
        connectors.append(RemoteHttpConnector(
            source_id=definition.source_id,
            institution_id=definition.institution_id,
            display_name=definition.display_name,
            base_url=definition.base_url,
            allowed_tools=frozenset(definition.allowed_tools),
            timeout_seconds=settings.connector_timeout_seconds,
            max_response_bytes=settings.connector_max_response_bytes,
            auth_token=definition.auth_token,
            scope_attestation_required=definition.scope_attestation_required,
        ))
        source_ids.add(definition.source_id)
    return ConnectorRegistry(tuple(connectors))


def build_runtime(settings: AppSettings | None = None, *, start_workers: bool = False) -> Runtime:
    settings = settings or AppSettings.from_env()
    settings.ensure_safe_for_production()
    sources = _build_sources(settings)
    source_ids = tuple(item.source_id for item in sources.all())
    tools = ToolRegistry((*build_college_tools(source_ids), *build_health_tools(source_ids)))
    connectors = _build_connectors(settings)
    store = _build_store(settings)
    try:
        store.prune_retention(settings.audit_retention_days)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not keep the API from starting
        logging.getLogger(__name__).warning("audit retention pruning skipped at start-up: %s", exc)
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
    platform = build_platform(settings, control_store=store, pdp=pdp, tracer=tracer, model=model, planner_model=_build_planner_model(settings, model), start_workers=start_workers) if settings.platform_enabled else None
    if platform is not None:
        # The intelligence stores open with the platform, so their retention runs here rather than beside the audit pruning above.
        try:
            platform.prune_intelligence(settings.intelligence_retention_days)
        except Exception as exc:  # noqa: BLE001 - housekeeping must not keep the API from starting
            logging.getLogger(__name__).warning("intelligence retention pruning skipped at start-up: %s", exc)
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
        platform=platform,
    )


def runtime_from_request(request: Request) -> Runtime:
    return request.app.state.runtime


def platform_from_request(request: Request) -> PlatformRuntime:
    from fastapi import HTTPException

    platform = request.app.state.runtime.platform
    if platform is None:
        raise HTTPException(status_code=503, detail="the institutional data platform is disabled (GURU_PLATFORM_ENABLED=false)")
    return platform


def principal_from_request(request: Request):
    runtime = runtime_from_request(request)
    if runtime.edge_identity is not None:
        try:
            assertion = request.headers.get("x-guru-edge-session", "")
            return runtime.edge_identity.resolve(assertion)
        except (PermissionError, ValueError, RuntimeError):
            return principal_from_headers({}, runtime.settings, runtime.auth_verifier)
    return principal_from_headers(request.headers, runtime.settings, runtime.auth_verifier)


__all__ = ["Runtime", "build_runtime", "platform_from_request", "principal_from_request", "runtime_from_request"]
