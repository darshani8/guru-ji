"""Production OIDC/JWT verification boundary.

The verifier validates the token before any role, capability, or institution
claim is converted into application authority. Token claims are untrusted data;
only explicitly mapped values are retained in the Principal model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jwt
from jwt import PyJWKClient

from ..config.settings import AppSettings
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType


class AuthenticationError(ValueError):
    """Safe authentication failure; details must not be returned to callers."""


_ROLE_ALIASES = {
    "student": PrincipalType.STUDENT,
    "faculty": PrincipalType.FACULTY,
    "main_admin": PrincipalType.MAIN_ADMIN,
    "admin": PrincipalType.MAIN_ADMIN,
}


def _anonymous() -> Principal:
    return Principal(
        principal_id="anonymous",
        principal_type=PrincipalType.ANONYMOUS,
        capabilities=frozenset(),
        scopes=(),
        authenticated=False,
    )


def _claim_values(claims: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = claims.get(name)
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set)):
        values = tuple(item for item in value if isinstance(item, str))
    else:
        values = ()
    return tuple(item.strip() for item in values if item.strip())


def _capabilities(claims: Mapping[str, Any], principal_type: PrincipalType) -> frozenset[Capability]:
    raw = _claim_values(claims, "guru_capabilities")
    if not raw:
        raw = _claim_values(claims, "capabilities")
    if raw:
        return frozenset(Capability(item) for item in raw if item in Capability._value2member_map_)
    if principal_type is PrincipalType.MAIN_ADMIN:
        return frozenset(Capability)
    if principal_type is PrincipalType.FACULTY:
        return frozenset({Capability.ASK_READ_ONLY, Capability.VIEW_SOURCE_METADATA, Capability.START_VOICE_SESSION})
    if principal_type is PrincipalType.STUDENT:
        return frozenset({Capability.ASK_READ_ONLY, Capability.START_VOICE_SESSION})
    return frozenset()


def _scopes(claims: Mapping[str, Any]) -> tuple[InstitutionScope, ...]:
    raw_scopes = claims.get("guru_scopes", claims.get("institution_scopes", ()))
    if isinstance(raw_scopes, Mapping):
        raw_scopes = (raw_scopes,)
    if not isinstance(raw_scopes, (list, tuple)):
        raw_scopes = ()
    scopes: list[InstitutionScope] = []
    for raw in raw_scopes:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("college_id"), str):
            continue
        try:
            scopes.append(
                InstitutionScope(
                    college_id=raw["college_id"],
                    department_id=raw.get("department_id") if isinstance(raw.get("department_id"), str) else None,
                    batch_id=raw.get("batch_id") if isinstance(raw.get("batch_id"), str) else None,
                )
            )
        except ValueError:
            continue
    if scopes:
        return tuple(scopes)

    # A single college claim is supported for simple institutional IdPs. It is
    # still explicit and never expands to another college.
    college_id = claims.get("college_id") or claims.get("college")
    if isinstance(college_id, str) and college_id.strip():
        return (InstitutionScope(college_id=college_id.strip()),)
    return ()


def principal_from_claims(claims: Mapping[str, Any]) -> Principal:
    """Map verified claims to the narrow application Principal model."""

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationError("token subject is missing")
    role_claim = claims.get("guru_role", claims.get("role", "student"))
    role = _ROLE_ALIASES.get(role_claim.lower(), PrincipalType.STUDENT) if isinstance(role_claim, str) else PrincipalType.STUDENT
    return Principal(
        principal_id=subject.strip(),
        principal_type=role,
        capabilities=_capabilities(claims, role),
        scopes=_scopes(claims),
        authenticated=True,
    )


class JwtVerifier:
    """Verify signed JWTs against an issuer's JWKS endpoint."""

    def __init__(self, settings: AppSettings, jwks_client: PyJWKClient | None = None) -> None:
        if not settings.oidc_jwks_url or not settings.oidc_issuer_url or not settings.oidc_audience:
            raise ValueError("OIDC issuer, audience, and JWKS URL are required")
        self.settings = settings
        self.jwks_client = jwks_client or PyJWKClient(settings.oidc_jwks_url)

    def verify(self, token: str) -> Principal:
        if not token.strip():
            raise AuthenticationError("bearer token is empty")
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(self.settings.oidc_algorithms),
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer_url,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
            return principal_from_claims(claims)
        except (jwt.PyJWTError, AuthenticationError, TypeError, ValueError) as exc:
            raise AuthenticationError("token verification failed") from exc


__all__ = ["AuthenticationError", "JwtVerifier", "principal_from_claims"]
