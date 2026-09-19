"""Safe HTTP error conversion."""

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.errors import GuruJiError


async def guruji_error_handler(request: Request, exc: GuruJiError) -> JSONResponse:
    error = exc.public_error
    content = {"error": {"code": error.code.value, "message": error.message, "request_id": error.request_id}}
    if error.retry_after_seconds is not None:
        content["error"]["retry_after_seconds"] = error.retry_after_seconds
    return JSONResponse(status_code=503, content=content)


__all__ = ["guruji_error_handler"]
