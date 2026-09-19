"""Environment-specific identity adapter for Guru Ji."""

from __future__ import annotations

from collections.abc import Mapping

from ..config.settings import AppSettings
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from .oidc import AuthenticationError, JwtVerifier


def _anonymous() -> Principal:
    return Principal(
        principal_id="anonymous",
        principal_type=PrincipalType.ANONYMOUS,
        capabilities=frozenset(),
        scopes=(),
        authenticated=False,
    )


def _capabilities_for(role: PrincipalType) -> frozenset[Capability]:
    if role is PrincipalType.MAIN_ADMIN:
        return frozenset(Capability)
    if role is PrincipalType.FACULTY:
        return frozenset({
            Capability.ASK_READ_ONLY,
            Capability.VIEW_SOURCE_METADATA,
            Capability.RUN_BRIEFING,
            Capability.VIEW_BRIEFING_HISTORY,
            Capability.START_VOICE_SESSION,
        })
    if role is PrincipalType.STUDENT:
        return frozenset({Capability.ASK_READ_ONLY, Capability.START_VOICE_SESSION})
    return frozenset()


def _principal_from_demo_headers(headers: Mapping[str, str], settings: AppSettings) -> Principal:
    authorization = headers.get("authorization", "")
    demo_id = headers.get("x-demo-principal", "")
    # Demo headers are identity fixtures, not an authentication mechanism. The
    # explicit development bearer token is required even in local/test mode.
    if authorization != f"Bearer {settings.dev_bearer_token}":
        return _anonymous()
    if not demo_id:
        demo_id = "demo-user"

    role_value = headers.get("x-demo-role", "student").lower()
    try:
        role = PrincipalType(role_value)
    except ValueError:
        role = PrincipalType.STUDENT
    college_id = headers.get("x-demo-college", "college_a")
    capabilities = set(_capabilities_for(role))
    explicit = headers.get("x-demo-capabilities", "")
    if explicit:
        capabilities = set()
        for raw in explicit.split(","):
            try:
                capabilities.add(Capability(raw.strip()))
            except ValueError:
                continue
    return Principal(
        principal_id=demo_id or "demo-user",
        principal_type=role,
        capabilities=frozenset(capabilities),
        scopes=(InstitutionScope(college_id=college_id),),
        authenticated=True,
    )


def principal_from_headers(
    headers: Mapping[str, str],
    settings: AppSettings,
    verifier: JwtVerifier | None = None,
) -> Principal:
    """Build a principal without granting authority to unverified claims.

    Development and test environments use the explicit demo headers. All other
    environments require a bearer JWT verified against the configured OIDC
    issuer and JWKS. Invalid or missing credentials become anonymous so routes
    can return a uniform 401 response without exposing verification details.
    """

    if settings.environment in {"development", "test"}:
        return _principal_from_demo_headers(headers, settings)

    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _anonymous()
    try:
        active_verifier = verifier or JwtVerifier(settings)
        return active_verifier.verify(token.strip())
    except (AuthenticationError, ValueError, RuntimeError):
        return _anonymous()


__all__ = ["principal_from_headers"]
