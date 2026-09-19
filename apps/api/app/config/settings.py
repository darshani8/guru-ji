"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..web_research.domain_allowlist import DEFAULT_ALLOWED_DOMAINS


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
    model_provider: str = "deterministic"
    ollama_base_url: str | None = None
    ollama_model_id: str = "llama3.1:8b"
    model_timeout_seconds: float = 8.0
    model_max_tokens: int = 800
    institution_connector_base_url: str | None = None
    institution_connector_source_id: str = "college_a_remote"
    institution_connector_institution_id: str = "college_a"
    institution_connector_display_name: str = "Configured institution connector"
    institution_connector_auth_token: str | None = field(default=None, repr=False)
    demo_data_enabled: bool = True
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
            model_provider=os.getenv("GURU_MODEL_PROVIDER", "deterministic").strip().lower(),
            ollama_base_url=os.getenv("GURU_OLLAMA_BASE_URL") or None,
            ollama_model_id=os.getenv("GURU_OLLAMA_MODEL_ID", "llama3.1:8b").strip(),
            model_timeout_seconds=float(os.getenv("GURU_MODEL_TIMEOUT_SECONDS", "8")),
            model_max_tokens=int(os.getenv("GURU_MODEL_MAX_TOKENS", "800")),
            institution_connector_base_url=os.getenv("GURU_INSTITUTION_CONNECTOR_BASE_URL") or None,
            institution_connector_source_id=os.getenv("GURU_INSTITUTION_CONNECTOR_SOURCE_ID", "college_a_remote").strip(),
            institution_connector_institution_id=os.getenv("GURU_INSTITUTION_CONNECTOR_INSTITUTION_ID", "college_a").strip(),
            institution_connector_display_name=os.getenv("GURU_INSTITUTION_CONNECTOR_DISPLAY_NAME", "Configured institution connector").strip(),
            institution_connector_auth_token=os.getenv("GURU_INSTITUTION_CONNECTOR_AUTH_TOKEN") or None,
            demo_data_enabled=os.getenv("GURU_ENABLE_DEMO_DATA", "false" if environment == "production" else "true").strip().lower() in {"1", "true", "yes", "on"},
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
        )

    def ensure_safe_for_production(self) -> None:
        if self.max_request_bytes <= 0:
            raise ValueError("GURU_MAX_REQUEST_BYTES must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("GURU_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.rate_limit_requests <= 0:
            raise ValueError("GURU_RATE_LIMIT_REQUESTS must be positive")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("GURU_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.model_provider not in {"deterministic", "ollama"}:
            raise ValueError("GURU_MODEL_PROVIDER must be deterministic or ollama")
        if self.model_timeout_seconds <= 0:
            raise ValueError("GURU_MODEL_TIMEOUT_SECONDS must be positive")
        if self.model_max_tokens <= 0:
            raise ValueError("GURU_MODEL_MAX_TOKENS must be positive")
        if self.model_provider == "ollama" and not self.ollama_base_url:
            raise ValueError("GURU_OLLAMA_BASE_URL is required when GURU_MODEL_PROVIDER=ollama")
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
        if not self.institution_connector_base_url:
            raise ValueError("production requires GURU_INSTITUTION_CONNECTOR_BASE_URL")
        if urlparse(self.institution_connector_base_url).scheme != "https":
            raise ValueError("production requires the institutional connector to use HTTPS")
        if not self.institution_connector_auth_token:
            raise ValueError("production requires GURU_INSTITUTION_CONNECTOR_AUTH_TOKEN")


__all__ = ["AppSettings"]
