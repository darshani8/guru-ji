"""Institution-local semantic connector service.

This service is intentionally narrow: authenticated API-to-connector calls can
read approved aggregate views, receive an effective-scope attestation, and
never perform institution writes or return raw identity records.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .repository import DemoReportingRepository, PostgresReportingRepository, ReportingRepository


APPROVED_TOOLS = frozenset({
    "institution.overview",
    "institution.attendance_summary",
    "institution.source_health",
})


def _env(name: str, default: str = "") -> str:
    """SAFFRON_<name>, or the GURU_<name> spelling a connector configured before the rename still sets."""

    value = os.getenv("SAFFRON_" + name)
    if value is None:
        value = os.getenv("GURU_" + name)
    return default if value is None else value


class ConnectorSettings:
    def __init__(self) -> None:
        self.environment = _env("CONNECTOR_ENVIRONMENT", _env("ENVIRONMENT", "development")).strip().lower()
        self.source_id = _env("CONNECTOR_SOURCE_ID", "college_a_remote").strip()
        self.institution_id = _env("CONNECTOR_INSTITUTION_ID", "college_a").strip()
        self.service_token = _env("CONNECTOR_SERVICE_TOKEN", "connector-dev-token").strip()
        self.database_url = _env("CONNECTOR_DATABASE_URL", "").strip()
        self.allowed_college_id = _env("CONNECTOR_ALLOWED_COLLEGE_ID", self.institution_id).strip()
        self.allowed_departments = frozenset(filter(None, (item.strip() for item in _env("CONNECTOR_ALLOWED_DEPARTMENTS", "").split(","))))
        self.allowed_batches = frozenset(filter(None, (item.strip() for item in _env("CONNECTOR_ALLOWED_BATCHES", "").split(","))))
        if not self.source_id or not self.institution_id or not self.service_token:
            raise ValueError("connector source, institution, and service token are required")
        if self.environment == "production" and not self.database_url:
            raise ValueError("production connector requires SAFFRON_CONNECTOR_DATABASE_URL")
        if self.environment == "production" and self.service_token == "connector-dev-token":
            raise ValueError("production connector must not use the development service token")

    @property
    def repository(self) -> ReportingRepository:
        if self.database_url:
            return PostgresReportingRepository(
                database_url=self.database_url,
                source_id=self.source_id,
                institution_id=self.institution_id,
            )
        if self.environment == "production":
            raise ValueError("production connector cannot use the demo repository")
        return DemoReportingRepository(source_id=self.source_id, institution_id=self.institution_id)


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    college_id: str
    department_id: str | None = None
    batch_id: str | None = None


class PrincipalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    scopes: list[ScopeBody] = Field(default_factory=list)
    consent_verified: bool = False
    revoked: bool = False


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_duration_ms: int = Field(default=5_000, ge=1, le=120_000)
    max_rows: int = Field(default=100, ge=1, le=1_000)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=10_000_000)


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["2"]
    source_id: str
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    request_id: str = Field(min_length=1, max_length=128)
    principal: PrincipalBody
    institution_scope: ScopeBody
    limits: LimitsBody = Field(default_factory=LimitsBody)


@dataclass(frozen=True, slots=True)
class EffectiveScope:
    college_id: str
    department_id: str | None
    batch_id: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "college_id": self.college_id,
            "department_id": self.department_id,
            "batch_id": self.batch_id,
        }


def _covers(grant: ScopeBody, requested: ScopeBody) -> bool:
    if grant.college_id != requested.college_id:
        return False
    if requested.department_id is None:
        if grant.department_id is not None:
            return False
    elif grant.department_id is not None and grant.department_id != requested.department_id:
        return False
    if requested.batch_id is None:
        if grant.batch_id is not None:
            return False
    elif grant.batch_id is not None and grant.batch_id != requested.batch_id:
        return False
    return True


def _effective_scope(settings: ConnectorSettings, body: ExecuteBody) -> EffectiveScope:
    requested = body.institution_scope
    if requested.college_id != settings.allowed_college_id:
        raise HTTPException(status_code=403, detail="requested college is outside connector scope")
    if settings.allowed_departments and requested.department_id not in settings.allowed_departments:
        raise HTTPException(status_code=403, detail="requested department is outside connector scope")
    if settings.allowed_batches and requested.batch_id not in settings.allowed_batches:
        raise HTTPException(status_code=403, detail="requested batch is outside connector scope")
    if not any(_covers(grant, requested) for grant in body.principal.scopes):
        raise HTTPException(status_code=403, detail="principal scope does not cover connector request")
    return EffectiveScope(requested.college_id, requested.department_id, requested.batch_id)


def _authorize(settings: ConnectorSettings, body: ExecuteBody, authorization: str | None) -> EffectiveScope:
    expected = f"Bearer {settings.service_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="connector service authentication failed")
    if body.source_id != settings.source_id:
        raise HTTPException(status_code=403, detail="source identity mismatch")
    if not body.principal.id or not body.principal.type:
        raise HTTPException(status_code=403, detail="verified principal identity is required")
    if body.tool_name not in APPROVED_TOOLS:
        raise HTTPException(status_code=403, detail="tool is not approved by connector contract")
    if body.principal.revoked:
        raise HTTPException(status_code=403, detail="revoked principal")
    if body.principal.type == "student" and body.institution_scope.batch_id and not body.principal.consent_verified:
        raise HTTPException(status_code=403, detail="verified consent is required for student batch scope")
    if "ask:read_only" not in body.principal.capabilities:
        raise HTTPException(status_code=403, detail="read-only capability is required")
    return _effective_scope(settings, body)


def create_app(settings: ConnectorSettings | None = None, repository: ReportingRepository | None = None) -> FastAPI:
    settings = settings or ConnectorSettings()
    repository = repository or settings.repository
    app = FastAPI(title="Agent Saffron Institution Connector", version="0.2.0")
    app.state.settings = settings
    app.state.repository = repository

    @app.get("/v1/health")
    def health() -> dict[str, object]:
        return {"service": "agentic-saffron-institution-connector", "source_id": settings.source_id, **repository.health()}

    @app.post("/v1/execute")
    def execute(body: ExecuteBody, authorization: str | None = Header(default=None)) -> dict[str, object]:
        effective = _authorize(settings, body, authorization)
        try:
            data = repository.execute(body.tool_name, body.arguments, effective.as_dict(), body.limits.max_rows)
        except KeyError as exc:
            raise HTTPException(status_code=403, detail="tool is not implemented by connector repository") from exc
        except Exception as exc:  # noqa: BLE001 - do not leak institution driver details
            raise HTTPException(status_code=503, detail="institution reporting repository unavailable") from exc
        now = datetime.now(timezone.utc)
        envelope: dict[str, object] = {
            "contract_version": "2",
            "source_id": settings.source_id,
            "tool_name": body.tool_name,
            "status": "success",
            "data": data,
            "effective_scope": effective.as_dict(),
            "provenance": [{
                "source_id": settings.source_id,
                "source_type": "internal_api",
                "retrieved_at": now.isoformat(),
                "complete": True,
                "rows_used": 1,
                "data_period": {"started_at": now.replace(month=1, day=1).isoformat(), "ended_at": now.isoformat()},
                "redactions_applied": ["identity_fields_not_selected", "aggregate_only"],
            }],
            "warnings": [],
        }
        if len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) > body.limits.max_response_bytes:
            raise HTTPException(status_code=413, detail="connector response exceeds caller limit")
        return envelope

    @app.get("/v1/ready")
    def ready() -> dict[str, object]:
        health_data = repository.health()
        if health_data.get("status") != "healthy":
            raise HTTPException(status_code=503, detail="connector repository is not ready")
        return {"ready": True, "source_id": settings.source_id}

    return app


app = create_app()

__all__ = ["APPROVED_TOOLS", "ConnectorSettings", "ExecuteBody", "app", "create_app"]
