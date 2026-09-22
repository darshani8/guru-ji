"""Data ingestion routes: upload existing institutional files, review mappings, commit."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...domain.principals import Capability
from ...ingestion.models import ParserError
from ...ingestion.service import IngestionError
from ...normalization.canonical import CANONICAL_ENTITIES
from ..dependencies import platform_from_request
from ._platform_common import require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])


class MappingDecisionBody(BaseModel):
    mapping: dict[str, str | None] = Field(default_factory=dict, description="Source header -> canonical field (null to leave unmapped)")
    entity: str | None = Field(default=None, max_length=40)
    remember: bool = True


class ReviewDecisionBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field(default="", max_length=500)


class SheetImportBody(BaseModel):
    sheet_url: str = Field(min_length=10, max_length=500)
    gid: str | None = Field(default=None, max_length=20)
    entity: str | None = Field(default=None, max_length=40)
    institution_id: str | None = Field(default=None, max_length=128)
    auto_commit: bool | None = None


# Parsing, normalisation, commits, object-store writes and queue publishes are
# synchronous and can take seconds; they run in the thread pool so the event
# loop keeps serving other requests (and the request timeout can still fire).


def _enqueue_processing(platform: Any, target: str, job_id: str, requested_by: str) -> str:
    return platform.jobs.enqueue(target, "ingestion.process", {"institution_id": target, "job_id": job_id, "requested_by": requested_by})


def _entity_or_422(entity: str | None) -> str | None:
    if entity and entity not in CANONICAL_ENTITIES:
        raise HTTPException(status_code=422, detail=f"unknown entity: {entity}; choose one of {', '.join(CANONICAL_ENTITIES)}")
    return entity


@router.get("/entities", summary="Describe the canonical data model files are mapped to")
async def list_entities(request: Request) -> dict[str, Any]:
    require_principal(request, Capability.DATA_INGEST)
    return {
        "entities": [
            {
                "name": entity.name, "description": entity.description, "natural_key": list(entity.natural_key),
                "fields": [{"name": item.name, "type": item.field_type.value, "required": item.required, "description": item.description, "contact": item.contact, "examples": list(item.synonyms[:5])} for item in entity.fields],
            }
            for entity in CANONICAL_ENTITIES.values()
        ]
    }


@router.post("/uploads", summary="Upload an existing institutional file (Excel, CSV, PDF, Word, scan, JSON)")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    entity: str | None = Form(default=None),
    sheet: str | None = Form(default=None),
    institution_id: str | None = Form(default=None),
    auto_commit: bool | None = Form(default=None),
    process: bool = Form(default=True),
) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    _entity_or_422(entity)
    content = await file.read()
    options: dict[str, Any] = {}
    if sheet:
        options["sheet"] = sheet
    if auto_commit is not None:
        options["auto_commit"] = auto_commit
    try:
        job = await run_in_threadpool(platform.ingestion.upload, target, principal.principal_id, file_name=file.filename or "upload.bin", content=content, content_type=file.content_type or "application/octet-stream", entity_hint=entity, options=options)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if process:
        job_id = await run_in_threadpool(_enqueue_processing, platform, target, job["job_id"], principal.principal_id)
        job = await run_in_threadpool(platform.store.get_job, target, job["job_id"]) or job
        job["background_job_id"] = job_id
    return {"job": _public_job(job)}


@router.post("/sheets", summary="Import a shared Google Sheet by URL")
async def import_sheet(body: SheetImportBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, body.institution_id)
    _entity_or_422(body.entity)
    if platform.sheets is None:
        raise HTTPException(status_code=503, detail="Google Sheets import is not configured")
    try:
        result = await run_in_threadpool(platform.sheets.fetch, body.sheet_url, gid=body.gid)
        options = {"auto_commit": body.auto_commit} if body.auto_commit is not None else {}
        job = await run_in_threadpool(platform.ingestion.create_job_from_parse_result, target, principal.principal_id, result, source_kind="google_sheets", entity_hint=body.entity, options=options)
    except (ParserError, IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await run_in_threadpool(_enqueue_processing, platform, target, job["job_id"], principal.principal_id)
    return {"job": _public_job(await run_in_threadpool(platform.store.get_job, target, job["job_id"]) or job)}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in ("job_id", "institution_id", "file_id", "entity", "status", "stage", "source_kind", "requested_by", "created_at", "updated_at", "row_count", "sheet_name", "error", "mapping", "report", "options", "background_job_id")}


@router.get("/jobs", summary="List ingestion jobs")
async def list_jobs(request: Request, institution_id: str | None = None, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    jobs = platform.store.list_jobs(target, limit=min(max(limit, 1), 200), status=status)
    return {"jobs": [_public_job(job) for job in jobs]}


@router.get("/jobs/{job_id}", summary="Inspect an ingestion job and its import report")
async def get_job(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    job = platform.store.get_job(target, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    return {"job": _public_job(job), "pending_reviews": platform.store.list_review_items(target, job_id=job_id, status="pending")}


@router.get("/jobs/{job_id}/records", summary="Staged rows for a job with their normalization and issues")
async def job_records(job_id: str, request: Request, institution_id: str | None = None, status: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    if platform.store.get_job(target, job_id) is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    rows = platform.store.job_records(target, job_id, limit=min(max(limit, 1), 1000), offset=max(offset, 0), status=status)
    return {"records": rows, "count": len(rows)}


@router.post("/jobs/{job_id}/mapping", summary="Approve or correct the proposed column mapping")
async def decide_mapping(job_id: str, body: MappingDecisionBody, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    _entity_or_422(body.entity)
    def apply_mapping() -> dict[str, Any]:
        # apply_mapping normalises and (with auto-commit) imports every staged row;
        # it awaits nothing, so it runs to completion on its own loop in the pool.
        return asyncio.run(platform.ingestion.apply_mapping(target, job_id, mapping=body.mapping, entity=body.entity, approved_by=principal.principal_id, remember=body.remember))

    try:
        job = await run_in_threadpool(apply_mapping)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    return {"job": _public_job(job)}


@router.post("/jobs/{job_id}/commit", summary="Import the staged rows into the canonical database")
async def commit_job(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    try:
        job = await run_in_threadpool(platform.ingestion.commit, target, job_id, committed_by=principal.principal_id)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    return {"job": _public_job(job)}


@router.get("/jobs/{job_id}/report", summary="Import report with lineage counts")
async def job_report(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    try:
        return platform.ingestion.import_report(target, job_id)
    except KeyError as exc:
        raise translate(exc) from exc


@router.get("/reviews", summary="Pending human-review items (mappings, duplicates)")
async def list_reviews(request: Request, institution_id: str | None = None, job_id: str | None = None, status: str | None = "pending", limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    return {"reviews": platform.store.list_review_items(target, job_id=job_id, status=status or None, limit=min(max(limit, 1), 500))}


@router.post("/reviews/{review_id}", summary="Resolve a duplicate review item")
async def resolve_review(review_id: str, body: ReviewDecisionBody, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    try:
        job = platform.ingestion.resolve_review(target, review_id, decision=body.decision, resolved_by=principal.principal_id, note=body.note)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    return {"job": _public_job(job)}


__all__ = ["router"]
