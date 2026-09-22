"""Public authentication configuration for the browser client.

The browser has to know whether to run an OIDC login or fall back to the local
demo identity, and it cannot infer that safely on its own. Only values that are
already public are published here: the issuer, the client identifier and the
requested scopes. No secret is exposed, and the mode mirrors exactly what
``principal_from_headers`` will accept, so the client cannot be told to use a
scheme the server would reject.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Kept in step with app.auth.principal.principal_from_headers, which only
# honours demo headers in these environments.
_DEMO_ENVIRONMENTS = {"development", "test"}


@router.get("/config", summary="Describe how the browser client should authenticate")
async def auth_config(request: Request) -> dict[str, object]:
    settings = request.app.state.runtime.settings
    if settings.environment in _DEMO_ENVIRONMENTS:
        return {"mode": "demo"}
    if not settings.oidc_issuer_url or not settings.oidc_audience:
        # The server will reject every caller as anonymous in this state. Say so
        # rather than letting the client start a login it cannot complete.
        return {"mode": "unavailable"}
    return {
        "mode": "oidc",
        "issuer": settings.oidc_issuer_url,
        "client_id": settings.oidc_audience,
        "scopes": ["openid", "email", "profile"],
    }


__all__ = ["router"]
