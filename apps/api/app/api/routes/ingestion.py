"""Data ingestion routes: upload existing institutional files, review mappings, commit."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...domain.principals import Capability
from ...ingestion.models import ParserError
from ...ingestion.service import JOB_PROCESSING, STAGE_IMPORTING, IngestionError, ProcessingError
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
# Reads go there too: a running import holds the store, and the console polls
# a job while it runs. Normalising and importing every row can outlast any
# request, so a route only checks and records a step, and the job queue runs it.


def _enqueue_processing(platform: Any, target: str, job_id: str, requested_by: str, *, force: bool = False, resume: bool = False) -> str:
    payload: dict[str, Any] = {"institution_id": target, "job_id": job_id, "requested_by": requested_by}
    if force:
        payload["force"] = True
    if resume:
        payload["resume"] = True
    try:
        return platform.jobs.enqueue(target, "ingestion.process", payload)
    except RuntimeError as exc:
        # The queue transport refused the job (for example SQS): the ingestion
        # job would otherwise look queued forever, so record the failure and
        # tell the caller which job to retry.
        platform.ingestion.record_unscheduled(target, job_id, f"processing could not be scheduled: {exc}")
        raise HTTPException(status_code=503, detail=f"ingestion job {job_id} could not be scheduled: {exc}; retry it once the job queue is available") from exc


def _hand_over(platform: Any, target: str, job: dict[str, Any], requested_by: str) -> dict[str, Any]:
    """Queue the step the service recorded (normalising, importing) and return the job as it now stands."""

    background_job_id = _enqueue_processing(platform, target, job["job_id"], requested_by, resume=True)
    job = platform.store.get_job(target, job["job_id"]) or job
    job["background_job_id"] = background_job_id
    return job


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
    except ProcessingError as exc:
        # A server-side failure (database, storage): the job state was recorded and the step can be retried.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
    public = {key: job.get(key) for key in ("job_id", "institution_id", "file_id", "entity", "status", "stage", "source_kind", "requested_by", "created_at", "updated_at", "row_count", "sheet_name", "error", "mapping", "report", "options", "background_job_id")}
    # The jobs other sheets of an uploaded workbook were queued as (known once the file was parsed).
    public["sibling_job_ids"] = [item["job_id"] for item in ((job.get("report") or {}).get("sheets") or {}).get("other_jobs") or []]
    return public


@router.get("/jobs", summary="List ingestion jobs")
async def list_jobs(request: Request, institution_id: str | None = None, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    jobs = await run_in_threadpool(platform.store.list_jobs, target, limit=min(max(limit, 1), 200), status=status)
    return {"jobs": [_public_job(job) for job in jobs]}


@router.get("/jobs/{job_id}", summary="Inspect an ingestion job and its import report")
async def get_job(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    job = await run_in_threadpool(platform.store.get_job, target, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    pending = await run_in_threadpool(platform.store.list_review_items, target, job_id=job_id, status="pending")
    return {"job": _public_job(job), "pending_reviews": pending}


@router.get("/jobs/{job_id}/records", summary="Staged rows for a job with their normalization and issues")
async def job_records(job_id: str, request: Request, institution_id: str | None = None, status: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    if await run_in_threadpool(platform.store.get_job, target, job_id) is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    rows = await run_in_threadpool(platform.store.job_records, target, job_id, limit=min(max(limit, 1), 1000), offset=max(offset, 0), status=status)
    return {"records": rows, "count": len(rows)}


@router.post("/jobs/{job_id}/mapping", summary="Approve or correct the proposed column mapping")
async def decide_mapping(job_id: str, body: MappingDecisionBody, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    _entity_or_422(body.entity)
    try:
        # Only the checks run here; the rows are normalised (and with auto-commit imported) on the job queue.
        job = await run_in_threadpool(platform.ingestion.approve_mapping, target, job_id, mapping=body.mapping, entity=body.entity, approved_by=principal.principal_id, remember=body.remember)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    return {"job": _public_job(await run_in_threadpool(_hand_over, platform, target, job, principal.principal_id))}


@router.post("/jobs/{job_id}/commit", summary="Import the staged rows into the canonical database")
async def commit_job(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    try:
        # Only the checks run here; the import itself runs on the job queue.
        job = await run_in_threadpool(platform.ingestion.request_commit, target, job_id, committed_by=principal.principal_id)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    return {"job": _public_job(await run_in_threadpool(_hand_over, platform, target, job, principal.principal_id))}


@router.post("/jobs/{job_id}/retry", summary="Re-run a failed or interrupted ingestion job", status_code=202)
async def retry_job(job_id: str, request: Request, institution_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Queue the job again; a run that is still alive (fresh heartbeat) is left untouched unless ``force`` is set."""

    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    job = await run_in_threadpool(platform.store.get_job, target, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    if job["status"] not in {"failed", "processing"}:
        raise HTTPException(status_code=409, detail=f"job cannot be retried from status {job['status']}")
    await run_in_threadpool(_enqueue_processing, platform, target, job_id, principal.principal_id, force=force)
    return {"job": _public_job(await run_in_threadpool(platform.store.get_job, target, job_id) or job)}


@router.get("/jobs/{job_id}/report", summary="Import report with lineage counts")
async def job_report(job_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_INGEST)
    target = resolve_institution(principal, institution_id)
    try:
        return await run_in_threadpool(platform.ingestion.import_report, target, job_id)
    except KeyError as exc:
        raise translate(exc) from exc


@router.get("/reviews", summary="Pending human-review items (mappings, duplicates)")
async def list_reviews(request: Request, institution_id: str | None = None, job_id: str | None = None, status: str | None = "pending", limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    return {"reviews": await run_in_threadpool(platform.store.list_review_items, target, job_id=job_id, status=status or None, limit=min(max(limit, 1), 500))}


@router.post("/reviews/{review_id}", summary="Resolve a duplicate review item")
async def resolve_review(review_id: str, body: ReviewDecisionBody, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DATA_REVIEW)
    target = resolve_institution(principal, institution_id)
    try:
        # Resolving the last duplicate may start the auto-commit: it is only requested here.
        job = await run_in_threadpool(platform.ingestion.resolve_review, target, review_id, decision=body.decision, resolved_by=principal.principal_id, note=body.note, defer_commit=True)
    except (IngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise translate(exc) from exc
    if (job["status"], job["stage"]) == (JOB_PROCESSING, STAGE_IMPORTING):
        job = await run_in_threadpool(_hand_over, platform, target, job, principal.principal_id)
    return {"job": _public_job(job)}


__all__ = ["router"]
