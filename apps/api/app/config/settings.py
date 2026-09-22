"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..config.institution_connectors import (
    InstitutionConnectorDefinition,
    parse_institution_connectors,
)
from ..web_research.domain_allowlist import DEFAULT_ALLOWED_DOMAINS


def _origins_from_env(name: str, *extra: str | None) -> tuple[str, ...]:
    """Collect scheme://host[:port] origins for the browser's connect policy.

    Anything carrying a path, credentials or a non-HTTP(S) scheme is dropped
    rather than rejected, so a malformed entry cannot widen the policy or take
    the service down at start-up.
    """

    candidates = [item.strip() for item in (os.getenv(name) or "").split(",")]
    candidates.extend(item for item in extra if item)
    origins: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlparse(candidate.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.username or parsed.password:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AppSettings:
    app_name: str = "guru-ji-api"
    version: str = "0.1.0"
    environment: str = "development"
    allowed_origins: tuple[str, ...] = ("http://localhost:5173",)
    dev_bearer_token: str = field(default="dev-token", repr=False)
    control_database_url: str | None = field(default="sqlite:///./data/guru_ji.db", repr=False)
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    # Origins the browser must reach to complete a login: the issuer's discovery
    # document and the provider's token endpoint, which for Cognito sits on a
    # different host than the issuer. Listed explicitly so the page's
    # Content-Security-Policy can stay closed to everything else.
    oidc_browser_origins: tuple[str, ...] = ()
    model_provider: str = "deterministic"
    ollama_base_url: str | None = None
    ollama_model_id: str = "llama3.1:8b"
    litellm_base_url: str | None = None
    litellm_model_id: str = ""
    litellm_api_key: str | None = field(default=None, repr=False)
    model_timeout_seconds: float = 8.0
    model_max_tokens: int = 800
    institution_connector_base_url: str | None = None
    institution_connector_source_id: str = "college_a_remote"
    institution_connector_institution_id: str = "college_a"
    institution_connector_display_name: str = "Configured institution connector"
    institution_connector_auth_token: str | None = field(default=None, repr=False)
    institution_connectors: tuple[InstitutionConnectorDefinition, ...] = ()
    connector_timeout_seconds: float = 5.0
    connector_max_response_bytes: int = 1_000_000
    connector_scope_attestation_required: bool = False
    demo_data_enabled: bool = True
    pdp_mode: str = "local"
    cerbos_url: str | None = None
    cerbos_policy_version: str = "guru-cerbos-v1"
    cerbos_timeout_seconds: float = 1.5
    cerbos_require_fresh: bool = True
    audit_fail_closed: bool = False
    audit_retention_days: int = 365
    otel_exporter_endpoint: str | None = None
    otel_exporter_timeout_seconds: float = 2.0
    web_search_provider: str = "disabled"
    web_search_endpoint: str = "https://api.tavily.com/search"
    web_search_api_key: str | None = field(default=None, repr=False)
    web_search_timeout_seconds: float = 8.0
    web_search_max_results: int = 5
    web_extract_timeout_seconds: float = 8.0
    web_extract_max_bytes: int = 1_000_000
    web_allowed_domains: tuple[str, ...] = tuple(sorted(DEFAULT_ALLOWED_DOMAINS))
    max_request_bytes: int = 1_000_000
    request_timeout_seconds: float = 30.0
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0
    edge_adapter: str = "disabled"
    edge_adapter_base_url: str | None = None
    edge_adapter_auth_token: str | None = field(default=None, repr=False)
    agent_gateway_url: str | None = None
    openfga_url: str | None = None
    livekit_url: str | None = None

    @classmethod
    def from_env(cls) -> "AppSettings":
        environment = os.getenv("GURU_ENVIRONMENT", "development").strip().lower()
        configured_origins = os.getenv("GURU_ALLOWED_ORIGINS")
        default_origins = "" if environment == "production" else "http://localhost:5173"
        origins = tuple(item.strip() for item in (configured_origins or default_origins).split(",") if item.strip())
        return cls(
            app_name=os.getenv("GURU_APP_NAME", "guru-ji-api"),
            version=os.getenv("GURU_VERSION", "0.1.0"),
            environment=environment,
            allowed_origins=origins,
            dev_bearer_token=os.getenv("GURU_DEV_BEARER_TOKEN", "dev-token"),
            control_database_url=os.getenv("CONTROL_DATABASE_URL", "sqlite:///./data/guru_ji.db"),
            oidc_issuer_url=os.getenv("GURU_OIDC_ISSUER_URL") or None,
            oidc_audience=os.getenv("GURU_OIDC_AUDIENCE") or None,
            oidc_jwks_url=os.getenv("GURU_OIDC_JWKS_URL") or None,
            oidc_algorithms=tuple(item.strip() for item in os.getenv("GURU_OIDC_ALGORITHMS", "RS256").split(",") if item.strip()),
            oidc_browser_origins=_origins_from_env("GURU_OIDC_BROWSER_ORIGINS", os.getenv("GURU_OIDC_ISSUER_URL")),
            model_provider=os.getenv("GURU_MODEL_PROVIDER", "deterministic").strip().lower(),
            ollama_base_url=os.getenv("GURU_OLLAMA_BASE_URL") or None,
            ollama_model_id=os.getenv("GURU_OLLAMA_MODEL_ID", "llama3.1:8b").strip(),
            litellm_base_url=os.getenv("GURU_LITELLM_BASE_URL") or None,
            litellm_model_id=os.getenv("GURU_LITELLM_MODEL_ID", "").strip(),
            litellm_api_key=os.getenv("GURU_LITELLM_API_KEY") or None,
            model_timeout_seconds=float(os.getenv("GURU_MODEL_TIMEOUT_SECONDS", "8")),
            model_max_tokens=int(os.getenv("GURU_MODEL_MAX_TOKENS", "800")),
            institution_connector_base_url=os.getenv("GURU_INSTITUTION_CONNECTOR_BASE_URL") or None,
            institution_connector_source_id=os.getenv("GURU_INSTITUTION_CONNECTOR_SOURCE_ID", "college_a_remote").strip(),
            institution_connector_institution_id=os.getenv("GURU_INSTITUTION_CONNECTOR_INSTITUTION_ID", "college_a").strip(),
            institution_connector_display_name=os.getenv("GURU_INSTITUTION_CONNECTOR_DISPLAY_NAME", "Configured institution connector").strip(),
            institution_connector_auth_token=os.getenv("GURU_INSTITUTION_CONNECTOR_AUTH_TOKEN") or None,
            institution_connectors=parse_institution_connectors(
                os.getenv("GURU_INSTITUTION_CONNECTORS"),
                environment=os.environ,
            ),
            connector_timeout_seconds=float(os.getenv("GURU_CONNECTOR_TIMEOUT_SECONDS", "5")),
            connector_max_response_bytes=int(os.getenv("GURU_CONNECTOR_MAX_RESPONSE_BYTES", "1000000")),
            connector_scope_attestation_required=_bool_env("GURU_CONNECTOR_SCOPE_ATTESTATION_REQUIRED", environment == "production"),
            demo_data_enabled=_bool_env("GURU_ENABLE_DEMO_DATA", environment != "production"),
            pdp_mode=os.getenv("GURU_PDP_MODE", "local").strip().lower(),
            cerbos_url=os.getenv("GURU_CERBOS_URL") or None,
            cerbos_policy_version=os.getenv("GURU_CERBOS_POLICY_VERSION", "guru-cerbos-v1").strip(),
            cerbos_timeout_seconds=float(os.getenv("GURU_CERBOS_TIMEOUT_SECONDS", "1.5")),
            cerbos_require_fresh=_bool_env("GURU_CERBOS_REQUIRE_FRESH", True),
            audit_fail_closed=_bool_env("GURU_AUDIT_FAIL_CLOSED", environment == "production"),
            audit_retention_days=int(os.getenv("GURU_AUDIT_RETENTION_DAYS", "365")),
            otel_exporter_endpoint=os.getenv("GURU_OTEL_EXPORTER_ENDPOINT") or None,
            otel_exporter_timeout_seconds=float(os.getenv("GURU_OTEL_EXPORTER_TIMEOUT_SECONDS", "2")),
            web_search_provider=os.getenv("GURU_WEB_SEARCH_PROVIDER", "disabled").strip().lower(),
            web_search_endpoint=os.getenv("GURU_WEB_SEARCH_ENDPOINT", "https://api.tavily.com/search").strip(),
            web_search_api_key=os.getenv("GURU_WEB_SEARCH_API_KEY") or None,
            web_search_timeout_seconds=float(os.getenv("GURU_WEB_SEARCH_TIMEOUT_SECONDS", "8")),
            web_search_max_results=int(os.getenv("GURU_WEB_SEARCH_MAX_RESULTS", "5")),
            web_extract_timeout_seconds=float(os.getenv("GURU_WEB_EXTRACT_TIMEOUT_SECONDS", "8")),
            web_extract_max_bytes=int(os.getenv("GURU_WEB_EXTRACT_MAX_BYTES", "1000000")),
            web_allowed_domains=tuple(item.strip().lower().rstrip(".") for item in os.getenv("GURU_WEB_ALLOWED_DOMAINS", ",".join(sorted(DEFAULT_ALLOWED_DOMAINS))).split(",") if item.strip()),
            max_request_bytes=int(os.getenv("GURU_MAX_REQUEST_BYTES", "1000000")),
            request_timeout_seconds=float(os.getenv("GURU_REQUEST_TIMEOUT_SECONDS", "30")),
            rate_limit_requests=int(os.getenv("GURU_RATE_LIMIT_REQUESTS", "120")),
            rate_limit_window_seconds=float(os.getenv("GURU_RATE_LIMIT_WINDOW_SECONDS", "60")),
            edge_adapter=os.getenv("GURU_EDGE_ADAPTER", "disabled").strip().lower(),
            edge_adapter_base_url=os.getenv("GURU_EDGE_ADAPTER_BASE_URL") or None,
            edge_adapter_auth_token=os.getenv("GURU_EDGE_ADAPTER_AUTH_TOKEN") or None,
            agent_gateway_url=os.getenv("GURU_AGENT_GATEWAY_URL") or None,
            openfga_url=os.getenv("GURU_OPENFGA_URL") or None,
            livekit_url=os.getenv("GURU_LIVEKIT_URL") or None,
        )

    def configured_institution_connectors(self) -> tuple[InstitutionConnectorDefinition, ...]:
        """Return all connector definitions, including the legacy single entry.

        The original single-connector environment variables remain supported so
        existing deployments do not change behavior when the multi-connector
        registry is not configured.
        """

        definitions = list(self.institution_connectors)
        if self.institution_connector_base_url:
            legacy = InstitutionConnectorDefinition(
                source_id=self.institution_connector_source_id,
                institution_id=self.institution_connector_institution_id,
                display_name=self.institution_connector_display_name,
                base_url=self.institution_connector_base_url,
                auth_token=self.institution_connector_auth_token,
                scope_attestation_required=self.connector_scope_attestation_required,
            )
            if any(item.source_id == legacy.source_id for item in definitions):
                raise ValueError(f"institution connector source ID is duplicated: {legacy.source_id}")
            definitions.append(legacy)
        return tuple(definitions)

    def ensure_safe_for_production(self) -> None:
        if self.max_request_bytes <= 0:
            raise ValueError("GURU_MAX_REQUEST_BYTES must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("GURU_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.rate_limit_requests <= 0:
            raise ValueError("GURU_RATE_LIMIT_REQUESTS must be positive")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("GURU_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.model_provider not in {"deterministic", "ollama", "litellm"}:
            raise ValueError("GURU_MODEL_PROVIDER must be deterministic, ollama, or litellm")
        if self.model_timeout_seconds <= 0:
            raise ValueError("GURU_MODEL_TIMEOUT_SECONDS must be positive")
        if self.model_max_tokens <= 0:
            raise ValueError("GURU_MODEL_MAX_TOKENS must be positive")
        if self.model_provider == "ollama" and not self.ollama_base_url:
            raise ValueError("GURU_OLLAMA_BASE_URL is required when GURU_MODEL_PROVIDER=ollama")
        if self.model_provider == "litellm" and not self.litellm_model_id:
            raise ValueError("GURU_LITELLM_MODEL_ID is required when GURU_MODEL_PROVIDER=litellm")
        if self.pdp_mode not in {"local", "cerbos"}:
            raise ValueError("GURU_PDP_MODE must be local or cerbos")
        if self.cerbos_timeout_seconds <= 0:
            raise ValueError("GURU_CERBOS_TIMEOUT_SECONDS must be positive")
        if not self.cerbos_policy_version.strip():
            raise ValueError("GURU_CERBOS_POLICY_VERSION must not be blank")
        if self.pdp_mode == "cerbos":
            if not self.cerbos_url:
                raise ValueError("GURU_CERBOS_URL is required when GURU_PDP_MODE=cerbos")
            parsed_cerbos = urlparse(self.cerbos_url)
            if parsed_cerbos.scheme not in {"http", "https"} or not parsed_cerbos.netloc:
                raise ValueError("GURU_CERBOS_URL must be an absolute HTTP(S) URL")
        if self.connector_timeout_seconds <= 0 or self.connector_max_response_bytes <= 0:
            raise ValueError("connector limits must be positive")
        if not 1 <= self.audit_retention_days <= 3650:
            raise ValueError("GURU_AUDIT_RETENTION_DAYS must be between 1 and 3650")
        if self.otel_exporter_timeout_seconds <= 0:
            raise ValueError("GURU_OTEL_EXPORTER_TIMEOUT_SECONDS must be positive")
        if self.otel_exporter_endpoint:
            parsed_otel = urlparse(self.otel_exporter_endpoint)
            if parsed_otel.scheme not in {"http", "https"} or not parsed_otel.netloc:
                raise ValueError("GURU_OTEL_EXPORTER_ENDPOINT must be an absolute HTTP(S) URL")
        if self.web_search_provider not in {"disabled", "tavily"}:
            raise ValueError("GURU_WEB_SEARCH_PROVIDER must be disabled or tavily")
        if self.web_search_timeout_seconds <= 0:
            raise ValueError("GURU_WEB_SEARCH_TIMEOUT_SECONDS must be positive")
        if self.web_extract_timeout_seconds <= 0:
            raise ValueError("GURU_WEB_EXTRACT_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.web_search_max_results <= 10:
            raise ValueError("GURU_WEB_SEARCH_MAX_RESULTS must be between 1 and 10")
        if self.web_extract_max_bytes <= 0:
            raise ValueError("GURU_WEB_EXTRACT_MAX_BYTES must be positive")
        if not self.web_allowed_domains:
            raise ValueError("GURU_WEB_ALLOWED_DOMAINS must contain at least one hostname")
        for domain in self.web_allowed_domains:
            normalized_domain = domain.strip().lower().rstrip(".")
            if not normalized_domain or any(character in normalized_domain for character in "/?#:"):
                raise ValueError("GURU_WEB_ALLOWED_DOMAINS must contain hostnames, not URLs")
            if urlparse(f"https://{normalized_domain}").hostname != normalized_domain:
                raise ValueError("GURU_WEB_ALLOWED_DOMAINS contains an invalid hostname")
        if self.web_search_provider == "tavily":
            if not self.web_search_api_key:
                raise ValueError("GURU_WEB_SEARCH_API_KEY is required when GURU_WEB_SEARCH_PROVIDER=tavily")
            parsed_search_url = urlparse(self.web_search_endpoint)
            if parsed_search_url.scheme not in {"http", "https"} or not parsed_search_url.netloc:
                raise ValueError("GURU_WEB_SEARCH_ENDPOINT must be an absolute HTTP(S) URL")
            if parsed_search_url.username or parsed_search_url.password or parsed_search_url.query or parsed_search_url.fragment:
                raise ValueError("GURU_WEB_SEARCH_ENDPOINT must not contain credentials, query, or fragment data")
        for field_name in ("institution_connector_source_id", "institution_connector_institution_id", "institution_connector_display_name"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.institution_connector_base_url:
            parsed_connector_url = urlparse(self.institution_connector_base_url)
            if parsed_connector_url.scheme not in {"http", "https"} or not parsed_connector_url.netloc:
                raise ValueError("GURU_INSTITUTION_CONNECTOR_BASE_URL must be an absolute HTTP(S) URL")
        self.configured_institution_connectors()
        if self.edge_adapter not in {"disabled", "openedx", "moodle"}:
            raise ValueError("GURU_EDGE_ADAPTER must be disabled, openedx, or moodle")
        if self.edge_adapter != "disabled" and not self.edge_adapter_base_url:
            raise ValueError("GURU_EDGE_ADAPTER_BASE_URL is required when an edge adapter is enabled")
        if self.edge_adapter != "disabled" and not self.edge_adapter_auth_token:
            raise ValueError("GURU_EDGE_ADAPTER_AUTH_TOKEN is required when an edge adapter is enabled")
        for field_name in ("edge_adapter_base_url", "agent_gateway_url", "openfga_url", "livekit_url"):
            endpoint = getattr(self, field_name)
            if endpoint:
                parsed_endpoint = urlparse(endpoint)
                if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
                    raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
                if parsed_endpoint.username or parsed_endpoint.password or parsed_endpoint.query or parsed_endpoint.fragment:
                    raise ValueError(f"{field_name} must not contain credentials, query, or fragment data")
        if self.environment != "production":
            return
        if self.web_search_provider == "tavily" and urlparse(self.web_search_endpoint).scheme != "https":
            raise ValueError("production requires the public-web search provider to use HTTPS")
        if self.dev_bearer_token == "dev-token":
            raise ValueError("the development bearer token must be replaced in production")
        if not self.allowed_origins:
            raise ValueError("GURU_ALLOWED_ORIGINS must be set in production")
        if not self.control_database_url or not self.control_database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires CONTROL_DATABASE_URL to use PostgreSQL")
        if not self.oidc_issuer_url or not self.oidc_audience or not self.oidc_jwks_url:
            raise ValueError("production requires OIDC issuer, audience, and JWKS URL settings")
        allowed_algorithms = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
        if not self.oidc_algorithms or any(item not in allowed_algorithms for item in self.oidc_algorithms):
            raise ValueError("production requires an explicit safe OIDC signing algorithm")
        if self.demo_data_enabled:
            raise ValueError("production cannot enable deterministic demo data")
        configured_connectors = self.configured_institution_connectors()
        if not configured_connectors:
            raise ValueError("production requires GURU_INSTITUTION_CONNECTOR_BASE_URL or GURU_INSTITUTION_CONNECTORS")
        for connector in configured_connectors:
            if urlparse(connector.base_url).scheme != "https":
                raise ValueError("production requires institutional connectors to use HTTPS")
            if not connector.auth_token:
                raise ValueError(f"production requires an auth token for connector {connector.source_id}")
            if not connector.scope_attestation_required:
                raise ValueError(f"production requires scope attestation for connector {connector.source_id}")
        if self.pdp_mode != "cerbos" or not self.cerbos_url:
            raise ValueError("production requires a Cerbos PDP")
        if urlparse(self.cerbos_url).scheme != "https":
            raise ValueError("production requires Cerbos over HTTPS")
        if not self.connector_scope_attestation_required:
            raise ValueError("production requires connector scope attestation")
        if self.model_provider == "deterministic":
            raise ValueError("production cannot use the deterministic provider")
        if not self.audit_fail_closed:
            raise ValueError("production requires fail-closed audit handling")


__all__ = ["AppSettings"]
