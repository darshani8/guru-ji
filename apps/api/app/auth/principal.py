"""Environment-specific identity adapter for Agent Saffron."""

from __future__ import annotations

from collections.abc import Mapping

from ..config.settings import AppSettings
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from .oidc import AuthenticationError, JwtVerifier
from .roles import capabilities_for_role, role_from_alias


def _anonymous() -> Principal:
    return Principal(
        principal_id="anonymous",
        principal_type=PrincipalType.ANONYMOUS,
        capabilities=frozenset(),
        scopes=(),
        authenticated=False,
    )


def _capabilities_for(role: PrincipalType) -> frozenset[Capability]:
    return capabilities_for_role(role)


def _principal_from_demo_headers(headers: Mapping[str, str], settings: AppSettings) -> Principal:
    authorization = headers.get("authorization", "")
    demo_id = headers.get("x-demo-principal", "")
    if authorization != f"Bearer {settings.dev_bearer_token}":
        return _anonymous()
    if not demo_id:
        demo_id = "demo-user"

    role = role_from_alias(headers.get("x-demo-role", "student"), PrincipalType.STUDENT)
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
    # Consent and revocation are deliberately not accepted from demo headers;
    # they must come from verified identity/control-plane state.
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
    """Build a principal without granting authority to unverified claims."""

    if settings.environment in {"development", "test"}:
        return _principal_from_demo_headers(headers, settings)

    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _anonymous()
    try:
        active_verifier = verifier or JwtVerifier(settings)
        principal = active_verifier.verify(token.strip())
        return principal if principal.active else _anonymous()
    except (AuthenticationError, ValueError, RuntimeError):
        return _anonymous()


__all__ = ["principal_from_headers"]
