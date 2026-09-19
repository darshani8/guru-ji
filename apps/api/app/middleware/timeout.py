"""Application-level timeout guard for bounded HTTP work."""

from __future__ import annotations

import asyncio

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .request_id import request_id_for


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, timeout_seconds: float = 30.0):
        super().__init__(app)
        if timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, request, call_next):
        if request.scope.get("type") != "http":
            return await call_next(request)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await call_next(request)
        except TimeoutError:
            request_id = request_id_for(request)
            return JSONResponse(
                status_code=504,
                content={
                    "error": {
                        "code": "request_timeout",
                        "message": "The request exceeded the configured time limit.",
                        "request_id": request_id,
                    }
                },
                headers={"X-Request-ID": request_id},
            )


__all__ = ["RequestTimeoutMiddleware"]
