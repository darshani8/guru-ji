"""Document intelligence routes: upload, list, search with citations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ...documents.evals import MAX_CASES, RagEvaluator
from ...domain.principals import Capability
from ...ingestion.models import ParserError
from ..dependencies import platform_from_request
from ._platform_common import require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1/documents", tags=["documents"])


class DocumentSearchBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    institution_id: str | None = Field(default=None, max_length=128)
    top_k: int = Field(default=5, ge=1, le=10)
    retrieve_k: int | None = Field(default=None, ge=1, le=50)
    category: str | None = Field(default=None, max_length=40)


class EvalCaseBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    case_id: str | None = Field(default=None, max_length=80)
    expected_document_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_pages: list[int] = Field(default_factory=list, max_length=50)
    expected_text: str | None = Field(default=None, max_length=500)
    reference_answer: str | None = Field(default=None, max_length=4000)
    must_contain: list[str] = Field(default_factory=list, max_length=20)
    answerable: bool = True
    category: str | None = Field(default=None, max_length=40)


class DocumentEvalBody(BaseModel):
    cases: list[EvalCaseBody] = Field(min_length=1, max_length=MAX_CASES)
    institution_id: str | None = Field(default=None, max_length=128)
    top_k: int = Field(default=5, ge=1, le=10)
    retrieve_k: int | None = Field(default=None, ge=1, le=50)


@router.post("", summary="Upload a policy, circular, or other document for retrieval")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    classification: str = Form(default="internal"),
    category: str = Form(default="general"),
    institution_id: str | None = Form(default=None),
) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DOCUMENTS_MANAGE)
    target = resolve_institution(principal, institution_id)
    content = await file.read()
    try:
        return await platform.documents.ingest(principal, target, file_name=file.filename or "document.bin", content=content, content_type=file.content_type or "application/octet-stream", title=title, classification=classification, category=category)
    except (ParserError, ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.get("", summary="List indexed documents visible to the caller")
async def list_documents(request: Request, institution_id: str | None = None, category: str | None = None, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DOCUMENTS_READ)
    target = resolve_institution(principal, institution_id)
    try:
        return {"documents": platform.documents.list(principal, target, limit=min(max(limit, 1), 200), category=category)}
    except PermissionError as exc:
        raise translate(exc) from exc


@router.post("/search", summary="Answer a question from documents with page-level sources")
async def search_documents(body: DocumentSearchBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DOCUMENTS_READ)
    target = resolve_institution(principal, body.institution_id)
    try:
        return await platform.documents.answer(principal, target, body.question, top_k=body.top_k, retrieve_k=body.retrieve_k, category=body.category)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.post("/evaluate", summary="Score retrieval, reranking and answers against labelled questions")
async def evaluate_documents(body: DocumentEvalBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DOCUMENTS_MANAGE)
    target = resolve_institution(principal, body.institution_id)
    evaluator = RagEvaluator(platform.documents)
    try:
        return await evaluator.evaluate(principal, target, [case.model_dump() for case in body.cases], top_k=body.top_k, retrieve_k=body.retrieve_k)
    except (ValueError, PermissionError) as exc:
        raise translate(exc) from exc


@router.delete("/{document_id}", summary="Remove a document and its index")
async def delete_document(document_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.DOCUMENTS_MANAGE)
    target = resolve_institution(principal, institution_id)
    try:
        deleted = platform.documents.delete(principal, target, document_id)
    except PermissionError as exc:
        raise translate(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="document not found")
    return {"deleted": True, "document_id": document_id}


__all__ = ["router"]
