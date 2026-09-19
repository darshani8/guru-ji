"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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
    max_request_bytes: int = 1_000_000

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
            max_request_bytes=int(os.getenv("GURU_MAX_REQUEST_BYTES", "1000000")),
        )

    def ensure_safe_for_production(self) -> None:
        if self.max_request_bytes <= 0:
            raise ValueError("GURU_MAX_REQUEST_BYTES must be positive")
        if self.model_provider not in {"deterministic", "ollama"}:
            raise ValueError("GURU_MODEL_PROVIDER must be deterministic or ollama")
        if self.model_timeout_seconds <= 0:
            raise ValueError("GURU_MODEL_TIMEOUT_SECONDS must be positive")
        if self.model_max_tokens <= 0:
            raise ValueError("GURU_MODEL_MAX_TOKENS must be positive")
        if self.model_provider == "ollama" and not self.ollama_base_url:
            raise ValueError("GURU_OLLAMA_BASE_URL is required when GURU_MODEL_PROVIDER=ollama")
        if self.environment != "production":
            return
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


__all__ = ["AppSettings"]
