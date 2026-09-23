"""Internet map routes: entities, assets and their evidence, seeding, and how the map measures up."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...domain.audit import AuditEvent, AuditOutcome
from ...domain.principals import Capability, Principal
from ..dependencies import platform_from_request, runtime_from_request
from ._platform_common import require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1/intelligence/map", tags=["intelligence-map"])


class SeedBody(BaseModel):
    sweep_tsv: str | None = Field(default=None, max_length=2_000_000, description="Sweep rows; omit to import the bundled 22 September 2026 sweep")
    lookalikes_tsv: str | None = Field(default=None, max_length=200_000)
    groups: list[str] | None = Field(default=None, max_length=50)
    all_groups: bool = False
    approved_by: str | None = Field(default=None, max_length=200)
    holdout_percent: int = Field(default=20, ge=0, le=50)
    institution_id: str | None = Field(default=None, max_length=128)


class HarvestBody(BaseModel):
    asset_ids: list[str] | None = Field(default=None, max_length=10)
    institution_id: str | None = Field(default=None, max_length=128)


class MapInstitutionBody(BaseModel):
    institution_id: str | None = Field(default=None, max_length=128)


class DecisionBody(BaseModel):
    decision: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=500)
    relation: Literal["official", "affiliated", "community", "third_party"] | None = None
    institution_id: str | None = Field(default=None, max_length=128)


class SourceBody(BaseModel):
    connector: str = Field(min_length=1, max_length=40)
    target: str = Field(min_length=1, max_length=1000)
    institution_id: str | None = Field(default=None, max_length=128)


def map_service(request: Request):
    platform = platform_from_request(request)
    if platform.intelligence_map is None:
        raise HTTPException(status_code=503, detail="the internet map is not enabled (set GURU_INTELLIGENCE_MAP_ENABLED=true)")
    return platform.intelligence_map


def audit_map_action(request: Request, principal: Principal, action: str, *, outcome: AuditOutcome = AuditOutcome.SUCCESS, metadata: dict[str, str | int | bool | None] | None = None) -> None:
    """Every change to the map is audited: who did what to which institution's map."""

    runtime_from_request(request).store.append_audit(AuditEvent(
        event_id=f"audit-{uuid4().hex}", event_type=f"intelligence.map.{action}", request_id=request.headers.get("x-request-id") or f"req-{uuid4().hex}", principal_id=principal.principal_id,
        endpoint=str(request.url.path), source_ids=("public_web",), tool_names=(f"intelligence.map.{action}",), outcome=outcome,
        decision_metadata=tuple(sorted((metadata or {}).items())),
    ))


@router.get("/entities", summary="Entities in the map (institutions, branches, units, look-alikes)")
async def entities(request: Request, institution_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        return {"entities": service.entities(principal, target, kind=kind)}
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.get("/assets", summary="Accounts, sites and pages in the map with their grades")
async def assets(
    request: Request, institution_id: str | None = None, platform: str | None = None, grade: str | None = None, relation: str | None = None, entity_id: str | None = None,
    status: str | None = None, limit: int = 200, offset: int = 0,
) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        rows = service.assets(principal, target, platform=platform, grade=grade, relation=relation, entity_id=entity_id, status=status, limit=min(max(limit, 1), 1000), offset=max(offset, 0))
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    return {"assets": rows, "untrusted_content": True}


@router.get("/assets/{asset_id}/evidence", summary="The evidence behind one asset's grade")
async def asset_evidence(asset_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return service.evidence(principal, target, asset_id)
    except (ValueError, PermissionError, KeyError) as exc:
        raise translate(exc) from exc


@router.get("/metrics", summary="Coverage and accuracy against the hidden ground truth (the ground truth and runs for managers only)")
async def metrics(request: Request, institution_id: str | None = None, kind: str | None = None, limit: int = 10) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        return service.metrics(principal, target, kind=kind, limit=limit)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/seed", summary="Import a sweep as unverified claims and set aside hidden ground truth")
async def seed(body: SeedBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    from ...internet_intelligence.map.seed import DEFAULT_LOOKALIKES, DEFAULT_SWEEP

    sweep = body.sweep_tsv if body.sweep_tsv is not None else DEFAULT_SWEEP.read_text(encoding="utf-8")
    lookalikes = body.lookalikes_tsv if body.lookalikes_tsv is not None else DEFAULT_LOOKALIKES.read_text(encoding="utf-8")
    try:
        # Thousands of rows are real database work: off the event loop.
        result = await run_in_threadpool(
            service.seed, principal, target, sweep_text=sweep, lookalikes_text=lookalikes, groups=body.groups, all_groups=body.all_groups, approved_by=body.approved_by,
            holdout_percent=body.holdout_percent, source="sweep" if body.sweep_tsv else "sweep-2026-09-22",
        )
    except (ValueError, PermissionError) as exc:
        audit_map_action(request, principal, "seed", outcome=AuditOutcome.DENIED if isinstance(exc, PermissionError) else AuditOutcome.FAILED, metadata={"institution_id": target})
        raise translate(exc) from exc
    audit_map_action(request, principal, "seed", metadata={
        "institution_id": target, "all_groups": body.all_groups, "groups": ",".join(result["summary"]["groups"])[:500], "approved_by": result["approved_by"],
        "needed_approval": bool(result["needed_approval"]), "assets_created": int(result["summary"]["assets_created"]),
    })
    return result


@router.post("/sync-profile", summary="Bring the intelligence profile's configured official domains into the map")
async def sync_profile(body: MapInstitutionBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    profile = platform_from_request(request).intelligence_store.get_profile(target)
    try:
        result = service.sync_profile(principal, target, profile)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "sync_profile", metadata={"institution_id": target, "domains_added": len(result["domains_added"])})
    return result


@router.post("/harvest", summary="Read official sites for the accounts they declare")
async def harvest(body: HarvestBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = await service.harvest(principal, target, asset_ids=body.asset_ids)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "harvest", metadata={"institution_id": target, "domains": int(result["domains"]), "new_assets": int(result["new_assets"])})
    return result


@router.post("/regrade", summary="Recompute every grade from the evidence log")
async def regrade(body: MapInstitutionBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.regrade(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "regrade", metadata={"institution_id": target, "changed": int(result["changed"])})
    return result


@router.get("/export", summary="The map as a tab-separated table in the manual sweep's format", response_class=PlainTextResponse)
async def export(request: Request, institution_id: str | None = None) -> PlainTextResponse:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        body = service.export(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    return PlainTextResponse(body, media_type="text/tab-separated-values; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="internet-map-{target}.tsv"'})


@router.post("/tick", summary="Run one map engine tick now (normally scheduled)")
async def tick(request: Request, body: MapInstitutionBody, background: bool = True) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        service.guard(principal, target, Capability.INTELLIGENCE_MANAGE)
        if background:
            job_id = platform_from_request(request).jobs.enqueue(target, "intelligence.map_tick", {"institution_id": target, "requested_by": principal.principal_id})
            audit_map_action(request, principal, "tick", metadata={"institution_id": target, "background": True})
            return {"accepted": True, "job_id": job_id}
        result = await service.tick(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "tick", metadata={"institution_id": target, "background": False, "sources": int(result.get("counts", {}).get("sources", 0))})
    return result


@router.get("/sources", summary="What the map watches and which leads it follows")
async def sources(request: Request, institution_id: str | None = None, status: str | None = None, connector: str | None = None, limit: int = 200) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return {"sources": service.sources(principal, target, status=status, connector=connector, limit=min(max(limit, 1), 1000))}
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/sources", summary="Point a page-reading connector at a URL (a directory or regulator listing)")
async def add_source(request: Request, body: SourceBody) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.add_source(principal, target, connector=body.connector, target=body.target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "add_source", metadata={"institution_id": target, "connector": body.connector, "created": bool(result["created"])})
    return result


@router.get("/review", summary="What a person must decide (managers only): suspected impersonators, disputes, court records, held runs")
async def review_queue(request: Request, institution_id: str | None = None, status: str | None = "open", kind: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return {**service.review_items(principal, target, status=status or None, kind=kind, limit=min(max(limit, 1), 500), offset=max(offset, 0)), "untrusted_content": True}
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/review/{review_id}/decision", summary="Decide a review item; the decision becomes evidence and is audited")
async def decide(review_id: str, body: DecisionBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.decide(principal, target, review_id, decision=body.decision, note=body.note, relation=body.relation)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="review item not found") from exc
    except (ValueError, PermissionError) as exc:
        audit_map_action(request, principal, "review_decision", outcome=AuditOutcome.DENIED if isinstance(exc, PermissionError) else AuditOutcome.FAILED, metadata={"institution_id": target, "review_id": review_id, "decision": body.decision})
        raise translate(exc) from exc
    audit_map_action(request, principal, "review_decision", metadata={"institution_id": target, "review_id": review_id, "kind": result["kind"], "decision": body.decision, "relation": body.relation})
    return result


@router.post("/rescore", summary="Recompute every grade; published only if the map's ground truth does not get worse")
async def rescore(request: Request, body: MapInstitutionBody) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = await run_in_threadpool(service.rescore, principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "rescore", metadata={"institution_id": target, "passed": bool(result["passed"])})
    return result


class IncidentBody(BaseModel):
    note: str = Field(default="", max_length=500)
    institution_id: str | None = Field(default=None, max_length=128)


@router.get("/incidents", summary="What went wrong with the institution's own presence, with what to do about it (managers only)")
async def incidents(request: Request, institution_id: str | None = None, status: str | None = None, severity: str | None = None, limit: int = 100) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return {"incidents": service.incidents(principal, target, status=status, severity=severity, limit=min(max(limit, 1), 500))}
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/incidents/{incident_id}/{action}", summary="Acknowledge or resolve an incident")
async def incident_action(incident_id: str, action: Literal["acknowledge", "resolve"], body: IncidentBody, request: Request) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.set_incident(principal, target, incident_id, status="acknowledged" if action == "acknowledge" else "resolved", note=body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, f"incident_{action}", metadata={"institution_id": target, "incident_id": incident_id, "kind": result.get("kind")})
    return {"incident": result}


@router.get("/digest", summary="Preview the map's daily digest (managers only)")
async def map_digest(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return service.digest(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/digest", summary="Send the map's daily digest to the alert recipients now (normally scheduled)")
async def send_map_digest(request: Request, body: MapInstitutionBody) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.digest(principal, target, send=True)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "digest_sent", metadata={"institution_id": target, "sent": bool(result["sent"])})
    return result


class SuppressBody(BaseModel):
    identifier: str = Field(min_length=1, max_length=1000, description="An account URL or web address to keep out of the map")
    reason: str = Field(min_length=1, max_length=200)
    institution_id: str | None = Field(default=None, max_length=128)


@router.post("/suppress", summary="Keep an account or address out of the map for good (only a keyed fingerprint is stored)")
async def suppress(request: Request, body: SuppressBody) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = service.suppress(principal, target, identifier=body.identifier, reason=body.reason)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    # The audit record names who suppressed and why, never what: that would undo the point.
    audit_map_action(request, principal, "suppress", metadata={"institution_id": target, "removed": bool(result["removed"]), "reason": body.reason[:200]})
    return result


@router.get("/ownership", summary="The institution's ownership token, how to publish it, and what the owner has confirmed (managers only)")
async def ownership(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return service.ownership(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/ownership/verify", summary="Check the official domains for the ownership token now")
async def verify_ownership(request: Request, body: MapInstitutionBody) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    try:
        result = await service.verify_ownership(principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc
    audit_map_action(request, principal, "ownership_verify", metadata={"institution_id": target, "verified": sum(item["outcome"] == "verified" for item in result["results"])})
    return result


@router.get("/summary", summary="The map at a glance: grades, the coverage grid, an estimate of how complete it is (more for managers)")
async def summary(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        return await run_in_threadpool(service.summary, principal, target)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.get("/connectors", summary="The connectors the engine may use and how each reaches the web")
async def connectors(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_READ)
    target = resolve_institution(principal, institution_id)
    try:
        return {"connectors": service.connectors(principal, target)}
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.get("/spend", summary="Today's spend against the platform caps and this institution's quota")
async def spend(request: Request, institution_id: str | None = None, day: str | None = None) -> dict[str, Any]:
    service = map_service(request)
    principal = require_principal(request, Capability.INTELLIGENCE_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        return service.spend(principal, target, day=day or datetime.now(timezone.utc).date().isoformat())
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


__all__ = ["audit_map_action", "map_service", "router"]
