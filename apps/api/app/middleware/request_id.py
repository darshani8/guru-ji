"""Request-id middleware for traceable API responses."""

from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id", f"req-{uuid4().hex}")
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

__all__ = ["RequestIdMiddleware"]
