"""Development identity adapter; production must replace this with OIDC/JWT validation."""

from __future__ import annotations

from collections.abc import Mapping

from ..config.settings import AppSettings
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType


def _capabilities_for(role: PrincipalType) -> frozenset[Capability]:
    if role is PrincipalType.MAIN_ADMIN:
        return frozenset(Capability)
    if role is PrincipalType.FACULTY:
        return frozenset({Capability.ASK_READ_ONLY, Capability.VIEW_SOURCE_METADATA, Capability.START_VOICE_SESSION})
    if role is PrincipalType.STUDENT:
        return frozenset({Capability.ASK_READ_ONLY, Capability.START_VOICE_SESSION})
    return frozenset()


def principal_from_headers(headers: Mapping[str, str], settings: AppSettings) -> Principal:
    authorization = headers.get("authorization", "")
    demo_id = headers.get("x-demo-principal", "")
    if not authorization and not demo_id:
        return Principal(
            principal_id="anonymous", principal_type=PrincipalType.ANONYMOUS,
            capabilities=frozenset(), scopes=(), authenticated=False,
        )
    if settings.environment not in {"development", "test"}:
        return Principal(
            principal_id="anonymous", principal_type=PrincipalType.ANONYMOUS,
            capabilities=frozenset(), scopes=(), authenticated=False,
        )
    if authorization and authorization != f"Bearer {settings.dev_bearer_token}":
        return Principal(
            principal_id="anonymous", principal_type=PrincipalType.ANONYMOUS,
            capabilities=frozenset(), scopes=(), authenticated=False,
        )
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
        principal_id=demo_id or "demo-user", principal_type=role,
        capabilities=frozenset(capabilities),
        scopes=(InstitutionScope(college_id=college_id),), authenticated=True,
    )


__all__ = ["principal_from_headers"]
