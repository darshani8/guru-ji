"""ASGI middleware for enforcing a bounded HTTP request size."""

from __future__ import annotations

from uuid import uuid4

from starlette.responses import JSONResponse


class RequestSizeLimitMiddleware:
    """Reject requests whose body exceeds the configured byte limit.

    The JSON/API layer also has field-level limits. This middleware provides the
    earlier outer boundary and handles both Content-Length and chunked bodies.
    It buffers at most ``max_bytes + 1`` bytes before handing a valid request to
    the application, so oversized input is never passed to route parsing.
    """

    def __init__(self, app, max_bytes: int, *, upload_max_bytes: int | None = None, upload_prefixes: tuple[str, ...] = ()) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if upload_max_bytes is not None and upload_max_bytes <= 0:
            raise ValueError("upload_max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes
        # File-upload routes get their own, larger bound; everything else keeps the tight default.
        self.upload_max_bytes = upload_max_bytes or max_bytes
        self.upload_prefixes = tuple(upload_prefixes)

    def _limit_for(self, scope) -> int:
        path = scope.get("path", "")
        if self.upload_prefixes and any(path.startswith(prefix) for prefix in self.upload_prefixes):
            return max(self.max_bytes, self.upload_max_bytes)
        return self.max_bytes

    @staticmethod
    def _header(scope, name: bytes) -> str | None:
        return next(
            (value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == name),
            None,
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._header(scope, b"x-request-id") or f"req-{uuid4().hex}"
        limit = self._limit_for(scope)
        content_length = self._header(scope, b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={"error": {"code": "invalid_request", "message": "Content-Length must be an integer", "request_id": request_id}},
                    headers={"x-request-id": request_id},
                )
                await response(scope, receive, send)
                return
            if declared_length < 0:
                response = JSONResponse(
                    status_code=400,
                    content={"error": {"code": "invalid_request", "message": "Content-Length cannot be negative", "request_id": request_id}},
                    headers={"x-request-id": request_id},
                )
                await response(scope, receive, send)
                return
            if declared_length > limit:
                response = JSONResponse(
                    status_code=413,
                    content={"error": {"code": "payload_too_large", "message": "request body exceeds the configured limit", "request_id": request_id}},
                    headers={"x-request-id": request_id},
                )
                await response(scope, receive, send)
                return

        messages = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.disconnect":
                break
            if message.get("type") != "http.request":
                break
            total += len(message.get("body", b""))
            if total > limit:
                response = JSONResponse(
                    status_code=413,
                    content={"error": {"code": "payload_too_large", "message": "request body exceeds the configured limit", "request_id": request_id}},
                    headers={"x-request-id": request_id},
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def buffered_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, buffered_receive, send)


__all__ = ["RequestSizeLimitMiddleware"]
