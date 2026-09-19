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
    max_request_bytes: int = 1_000_000

    @classmethod
    def from_env(cls) -> "AppSettings":
        origins = tuple(item.strip() for item in os.getenv("GURU_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if item.strip())
        return cls(
            app_name=os.getenv("GURU_APP_NAME", "guru-ji-api"),
            version=os.getenv("GURU_VERSION", "0.1.0"),
            environment=os.getenv("GURU_ENVIRONMENT", "development"),
            allowed_origins=origins,
            dev_bearer_token=os.getenv("GURU_DEV_BEARER_TOKEN", "dev-token"),
            control_database_url=os.getenv("CONTROL_DATABASE_URL", "sqlite:///./data/guru_ji.db"),
            max_request_bytes=int(os.getenv("GURU_MAX_REQUEST_BYTES", "1000000")),
        )

    def ensure_safe_for_production(self) -> None:
        if self.environment == "production" and self.dev_bearer_token == "dev-token":
            raise ValueError("the development bearer token must be replaced in production")


__all__ = ["AppSettings"]
