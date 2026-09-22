"""Security headers that do not depend on a reverse proxy configuration."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.middleware.base import BaseHTTPMiddleware


def _content_security_policy(auth_origins: Sequence[str]) -> str:
    # The browser has to fetch the identity provider's discovery document and
    # POST to its token endpoint, and `connect-src` falls back to `default-src`
    # when unset. Naming those origins keeps the login working without opening
    # the page to anything else.
    connect_src = " ".join(("'self'", *auth_origins))
    return (
        "default-src 'self'; "
        f"connect-src {connect_src}; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, production: bool = False, auth_origins: Sequence[str] = ()):
        super().__init__(app)
        self.production = production
        self.content_security_policy = _content_security_policy(tuple(auth_origins))

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", self.content_security_policy)
        if self.production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


__all__ = ["SecurityHeadersMiddleware"]
