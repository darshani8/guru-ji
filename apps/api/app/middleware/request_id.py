"""Request-id middleware for traceable API responses."""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware


_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def request_id_for(request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    candidate = request.headers.get("x-request-id", "")
    request_id = candidate if _SAFE_REQUEST_ID.fullmatch(candidate) else f"req-{uuid4().hex}"
    request.state.request_id = request_id
    return request_id


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request_id_for(request)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


__all__ = ["RequestIdMiddleware", "request_id_for"]
