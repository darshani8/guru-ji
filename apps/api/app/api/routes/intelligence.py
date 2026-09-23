"""Internet intelligence routes: profile, investigate, mentions, digest, monitoring."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...domain.principals import Capability
from ...internet_intelligence.search import IntelligenceSearchUnavailable
from ...internet_intelligence.service import InvestigationQuotaExceeded
from ..dependencies import platform_from_request
from ._platform_common import require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1/intelligence", tags=["intelligence"])


class ProfileBody(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    location: str = Field(default="", max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    official_domains: list[str] = Field(default_factory=list, max_length=20)
    programs: list[str] = Field(default_factory=list, max_length=50)
    social_accounts: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    exclusions: list[str] = Field(default_factory=list, max_length=20)
    monitoring_enabled: bool = False
    alert_recipients: list[str] = Field(default_factory=list, max_length=20)
    security_contacts: list[str] = Field(default_factory=list, max_length=10, description="Email addresses of whoever runs the institution's sites; high-severity incidents are emailed to them at once")
    institution_id: str | None = Field(default=None, max_length=128)


class InvestigateBody(BaseModel):
    question: str | None = Field(default=None, max_length=300)
    window_days: int = Field(default=7, ge=1, le=365)
    topics: list[str] | None = Field(default=None, max_length=10)
    max_results: int = Field(default=10, ge=1, le=30)
    institution_id: str | None = Field(default=None, max_length=128)


def _service(request: Request):
    platform = platform_from_request(request)
    if platform.intelligence is None:
        raise HTTPException(status_code=503, detail="internet intelligence is not configured (set GURU_INTELLIGENCE_SEARCH_PROVIDER)")
    return platform


@router.get("/profile", summary="The institution's intelligence profile")
async def get_profile(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    profile = platform.intelligence_store.get_profile(target)
    return {"profile": profile.as_dict() if profile else None, "configured": platform.intelligence is not None}


@router.put("/profile", summary="Create or update the intelligence profile")
async def put_profile(body: ProfileBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    from ...internet_intelligence.profile import InstitutionProfile

    try:
        profile = InstitutionProfile.from_dict(target, body.model_dump(exclude={"institution_id"}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    platform.intelligence_store.save_profile(profile, updated_by=principal.principal_id)
    return {"profile": profile.as_dict()}


@router.post("/investigate", summary="Search public sources about the institution now")
async def investigate(body: InvestigateBody, request: Request) -> dict[str, Any]:
    platform = _service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, body.institution_id)
    try:
        return await platform.intelligence.investigate(principal, target, question=body.question, window_days=body.window_days, topics=body.topics, max_results=body.max_results)  # type: ignore[union-attr]
    except InvestigationQuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except IntelligenceSearchUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.get("/mentions", summary="Stored public mentions (evidence records)")
async def mentions(request: Request, institution_id: str | None = None, days: int | None = None, source_type: str | None = None, status: str | None = "kept", limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    manager = principal.has_capability(Capability.INTELLIGENCE_MANAGE)
    if not manager and status != "kept":
        # Items held for review or excluded (low-confidence matches, other
        # institutions, people) are working material for managers.
        raise HTTPException(status_code=403, detail="only kept mentions are available without intelligence:manage")
    rows = platform.intelligence_store.list_documents(target, days=days, status=status or None, source_type=source_type, limit=min(max(limit, 1), 500), exclude_sensitive=not manager)
    return {"mentions": rows, "untrusted_content": True, "sensitive_hidden": not manager}


@router.get("/digest", summary="Daily intelligence digest from continuous monitoring")
async def digest(request: Request, institution_id: str | None = None, days: int = 1) -> dict[str, Any]:
    platform = _service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    return platform.intelligence.digest(principal, target, days=min(max(days, 1), 30))  # type: ignore[union-attr]


@router.post("/monitor/run", summary="Run a monitoring pass now (normally scheduled)")
async def run_monitor(request: Request, institution_id: str | None = None, background: bool = True) -> dict[str, Any]:
    platform = _service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    if platform.monitor is None:
        raise HTTPException(status_code=503, detail="monitoring is not configured")
    if background:
        job_id = platform.jobs.enqueue(target, "intelligence.monitor", {"institution_id": target, "requested_by": principal.principal_id})
        return {"accepted": True, "job_id": job_id}
    try:
        return await platform.monitor.run_for(target)
    except (ValueError, IntelligenceSearchUnavailable) as exc:
        raise translate(exc) from exc


@router.get("/reports", summary="Past investigation reports")
async def reports(request: Request, institution_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    return {"reports": platform.intelligence_store.list_reports(target, limit=min(max(limit, 1), 100), include_sensitive=principal.has_capability(Capability.INTELLIGENCE_MANAGE))}


__all__ = ["router"]
