"""Safe HTTP error conversion with request correlation."""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..domain.errors import GuruJiError


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None) or request.headers.get("x-request-id")


def _response(request: Request, status_code: int, code: str, message: str, *, details=None) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    request_id = _request_id(request)
    if request_id:
        error["request_id"] = request_id
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


async def guruji_error_handler(request: Request, exc: GuruJiError) -> JSONResponse:
    error = exc.public_error
    error_payload: dict[str, object] = {
        "code": error.code.value,
        "message": error.message,
        "request_id": error.request_id,
    }
    if error.retry_after_seconds is not None:
        error_payload["retry_after_seconds"] = error.retry_after_seconds
    content: dict[str, object] = {"error": error_payload}
    return JSONResponse(status_code=503, content=content)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
    code_by_status = {
        400: "bad_request",
        401: "authentication_required",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limit_exceeded",
        500: "internal_error",
        503: "service_unavailable",
    }
    response = _response(request, exc.status_code, code_by_status.get(exc.status_code, "http_error"), detail)
    for header_name, header_value in (exc.headers or {}).items():
        response.headers[header_name] = header_value
    return response


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "location": list(error.get("loc", ())),
            "message": str(error.get("msg", "invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()
    ]
    return _response(
        request,
        422,
        "request_validation_failed",
        "The request did not satisfy the API contract.",
        details=details,
    )


__all__ = ["guruji_error_handler", "http_exception_handler", "validation_exception_handler"]
