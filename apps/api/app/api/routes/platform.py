"""Institution registry, generated reports, and notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...actions.files import FORMAT_CONTENT_TYPES
from ...domain.principals import Capability, InstitutionScope
from ...storage.object_store import ObjectStoreError
from ..dependencies import platform_from_request
from ._platform_common import require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1", tags=["platform"])


class InstitutionBody(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    location: str = Field(default="", max_length=120)
    timezone: str = Field(default="Asia/Kolkata", max_length=60)
    email_domains: list[str] = Field(default_factory=list, max_length=20)
    settings: dict[str, Any] = Field(default_factory=dict)


@router.get("/institutions", summary="Institutions registered on the platform (scoped to the caller)")
async def list_institutions(request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    rows = platform.store.list_institutions()
    if principal.principal_type.value != "platform_super_admin":
        rows = [row for row in rows if principal.can_access(InstitutionScope(str(row["institution_id"])))]
    return {"institutions": [{key: row.get(key) for key in ("institution_id", "name", "location", "timezone", "status", "created_at", "updated_at")} for row in rows]}


@router.put("/institutions/{institution_id}", summary="Register or update an institution")
async def put_institution(institution_id: str, body: InstitutionBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.MANAGE_ACCESS)
    target = resolve_institution(principal, institution_id)
    settings = dict(body.settings)
    if body.email_domains:
        settings["email_domains"] = [item.strip().lower() for item in body.email_domains if item.strip()]
    record = platform.store.upsert_institution(target, body.name, location=body.location, timezone_name=body.timezone, settings=settings)
    return {"institution": {key: record.get(key) for key in ("institution_id", "name", "location", "timezone", "status", "settings", "created_at", "updated_at")}}


@router.get("/institutions/{institution_id}", summary="Institution details and record counts")
async def get_institution(institution_id: str, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    record = platform.store.get_institution(target)
    return {"institution": record, "record_counts": platform.store.entity_counts(target)}


@router.get("/reports", summary="Generated reports")
async def list_reports(request: Request, institution_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    try:
        return {"reports": await run_in_threadpool(platform.reports.list, principal, target, limit=min(max(limit, 1), 200))}
    except PermissionError as exc:
        raise translate(exc) from exc


@router.get("/reports/{report_id}/download", summary="Download a generated report")
async def download_report(report_id: str, request: Request, institution_id: str | None = None) -> Response:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    try:
        # The object-store read (S3 in production) blocks; keep it off the event loop.
        record, content = await run_in_threadpool(platform.reports.fetch, principal, target, report_id)
    except (PermissionError, KeyError) as exc:
        raise translate(exc) from exc
    except ObjectStoreError as exc:
        # The record exists but its file is gone (expired, deleted, or an in-memory store after a restart).
        raise HTTPException(status_code=410, detail=f"the file for report {report_id} is no longer available; generate the report again") from exc
    file_name = str(record["object_key"]).rsplit("/", 1)[-1]
    return Response(content=content, media_type=FORMAT_CONTENT_TYPES.get(str(record["format"]), "application/octet-stream"), headers={"Content-Disposition": f'attachment; filename="{file_name}"'})


@router.get("/notifications", summary="The caller's notifications")
async def list_notifications(request: Request, institution_id: str | None = None, unread_only: bool = False, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return {"notifications": platform.notifications.inbox(principal, target, limit=min(max(limit, 1), 200), unread_only=unread_only)}


@router.post("/notifications/{notification_id}/read", summary="Mark a notification as read")
async def mark_read(notification_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    if not platform.notifications.mark_read(principal, target, notification_id):
        raise HTTPException(status_code=404, detail="notification not found")
    return {"read": True}


@router.get("/emails", summary="Email outbox for the institution")
async def list_emails(request: Request, institution_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.ACTIONS_EMAIL)
    target = resolve_institution(principal, institution_id)
    rows = platform.store.list_emails(target, limit=min(max(limit, 1), 200))
    return {"emails": [{key: row.get(key) for key in ("email_id", "recipients", "subject", "attachments", "status", "provider", "created_by", "created_at", "sent_at", "error")} for row in rows]}


__all__ = ["router"]
