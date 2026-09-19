"""Small in-process rate limiter for the single-process reference deployment."""

from __future__ import annotations

from collections import defaultdict, deque
from math import ceil
from threading import Lock
from time import monotonic

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .request_id import request_id_for


_EXEMPT_PATHS = {
    "/",
    "/favicon.ico",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/v1/health/live",
    "/v1/health/ready",
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply a bounded sliding-window limit per client and route.

    This protects the local reference server and is deliberately not presented
    as a distributed production quota. A production deployment should also
    enforce limits at its gateway or ingress layer.
    """

    def __init__(self, app, *, max_requests: int = 120, window_seconds: float = 60.0):
        super().__init__(app)
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("rate-limit settings must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    @staticmethod
    def _client_key(request) -> str:
        host = request.client.host if request.client else "unknown"
        return f"{host}:{request.url.path}"

    def _allow(self, key: str, now: float) -> tuple[bool, int, int]:
        with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = max(1, ceil(self.window_seconds - (now - hits[0])))
                return False, 0, retry_after
            hits.append(now)
            return True, max(0, self.max_requests - len(hits)), 0

    async def dispatch(self, request, call_next):
        if request.scope.get("type") != "http" or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        allowed, remaining, retry_after = self._allow(self._client_key(request), monotonic())
        if not allowed:
            request_id = request_id_for(request)
            response = JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Too many requests for this route; retry later.",
                        "request_id": request_id,
                    }
                },
                headers={"Retry-After": str(retry_after), "X-Request-ID": request_id},
            )
            response.headers["X-RateLimit-Limit"] = str(self.max_requests)
            response.headers["X-RateLimit-Remaining"] = "0"
            return response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


__all__ = ["RateLimitMiddleware"]
