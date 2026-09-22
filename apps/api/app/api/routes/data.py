"""Unified Data Access API over the canonical institution database."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...data_access.service import DataAccessDenied
from ..dependencies import platform_from_request
from ._platform_common import require_principal, resolve_institution

router = APIRouter(prefix="/v1/data", tags=["data"])


def _run(callback):
    try:
        return callback()
    except DataAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/summary", summary="Institution headline indicators")
async def summary(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.institution_summary(principal, target))


@router.get("/students", summary="Find students with minimal fields")
async def students(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, department: str | None = None, section: str | None = None, name_contains: str | None = None, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.find_students(principal, target, program=program, semester=semester, department=department, section=section, name_contains=name_contains, status=status, limit=limit))


@router.get("/students/count", summary="Count students")
async def students_count(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, department: str | None = None, status: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.count_students(principal, target, program=program, semester=semester, department=department, status=status))


@router.get("/students/{student_id}", summary="One student record")
async def student(student_id: str, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    record = _run(lambda: platform.data.get_student(principal, target, student_id))
    if record is None:
        raise HTTPException(status_code=404, detail="student not found")
    return {"student": record}


@router.get("/attendance/low", summary="Students below an attendance threshold")
async def attendance_low(request: Request, institution_id: str | None = None, threshold: float = 75.0, program: str | None = None, semester: str | None = None, department: str | None = None, period: str | None = None, limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.low_attendance(principal, target, threshold=threshold, program=program, semester=semester, department=department, period=period, limit=limit))


@router.get("/attendance/summary", summary="Attendance aggregates")
async def attendance_summary(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, department: str | None = None, period: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.attendance_summary(principal, target, program=program, semester=semester, department=department, period=period))


@router.get("/fees/pending", summary="Students with pending fees")
async def fees_pending(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, academic_year: str | None = None, limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.pending_fees(principal, target, program=program, semester=semester, academic_year=academic_year, limit=limit))


@router.get("/fees/summary", summary="Fee collection summary")
async def fees_summary(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, academic_year: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.fee_summary(principal, target, program=program, semester=semester, academic_year=academic_year))


@router.get("/exams/summary", summary="Exam performance summary")
async def exams_summary(request: Request, institution_id: str | None = None, program: str | None = None, semester: str | None = None, course_code: str | None = None, exam_name: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.exam_summary(principal, target, program=program, semester=semester, course_code=course_code, exam_name=exam_name))


@router.get("/programs", summary="Programs offered")
async def programs(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.list_programs(principal, target))


@router.get("/departments", summary="Departments and heads")
async def departments(request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.list_departments(principal, target))


@router.get("/faculty", summary="Faculty directory")
async def faculty(request: Request, institution_id: str | None = None, department: str | None = None, designation: str | None = None, name_contains: str | None = None, limit: int = 100) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.find_faculty(principal, target, department=department, designation=designation, name_contains=name_contains, limit=limit))


@router.get("/events", summary="Upcoming events")
async def events(request: Request, institution_id: str | None = None, days: int = 30, limit: int = 50) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.upcoming_events(principal, target, days=days, limit=limit))


@router.get("/admissions/summary", summary="Admission applications by status")
async def admissions(request: Request, institution_id: str | None = None, program: str | None = None, academic_year: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    target = resolve_institution(principal, institution_id)
    return _run(lambda: platform.data.admissions_summary(principal, target, program=program, academic_year=academic_year))


__all__ = ["router"]
