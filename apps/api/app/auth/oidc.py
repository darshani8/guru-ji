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


# Cognito exposes user-pool custom attributes under a "custom:" prefix, so a
# claim configured as guru_role arrives as custom:guru_role. Accepting both
# spellings keeps the provider from needing a token-rewriting Lambda purely to
# strip the prefix. The prefix carries no authority of its own: these values
# are still only read from a token whose signature has already been verified.
_CUSTOM_PREFIX = "custom:"


def _lookup(claims: Mapping[str, Any], name: str) -> Any:
    if name in claims:
        return claims[name]
    return claims.get(_CUSTOM_PREFIX + name)


def _claim_values(claims: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = _lookup(claims, name)
    values: tuple[str, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set)):
        values = tuple(item for item in value if isinstance(item, str))
    else:
        values = ()
    return tuple(item.strip() for item in values if item.strip())


def _truthy_claim(claims: Mapping[str, Any], *names: str) -> bool:
    for name in names:
        value = _lookup(claims, name)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "verified", "active"}
    return False


def _capabilities(claims: Mapping[str, Any], principal_type: PrincipalType) -> frozenset[Capability]:
    raw = _claim_values(claims, "guru_capabilities")
    if not raw:
        raw = _claim_values(claims, "capabilities")
    if raw:
        return frozenset(Capability(item) for item in raw if item in Capability._value2member_map_)
    if principal_type is PrincipalType.MAIN_ADMIN:
        return frozenset(Capability)
    if principal_type is PrincipalType.FACULTY:
        return frozenset({
            Capability.ASK_READ_ONLY,
            Capability.VIEW_SOURCE_METADATA,
            Capability.RUN_BRIEFING,
            Capability.VIEW_BRIEFING_HISTORY,
            Capability.START_VOICE_SESSION,
        })
    if principal_type is PrincipalType.STUDENT:
        return frozenset({Capability.ASK_READ_ONLY, Capability.START_VOICE_SESSION})
    return frozenset()


def _scopes(claims: Mapping[str, Any]) -> tuple[InstitutionScope, ...]:
    raw_scopes = _lookup(claims, "guru_scopes")
    if raw_scopes is None:
        raw_scopes = _lookup(claims, "institution_scopes") or ()
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

    college_id = _lookup(claims, "college_id") or _lookup(claims, "college")
    if isinstance(college_id, str) and college_id.strip():
        return (InstitutionScope(college_id=college_id.strip()),)
    return ()


def principal_from_claims(claims: Mapping[str, Any]) -> Principal:
    """Map verified claims to the narrow application Principal model."""

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationError("token subject is missing")
    role_claim = _lookup(claims, "guru_role")
    if not isinstance(role_claim, str) or not role_claim.strip():
        role_claim = _lookup(claims, "role")
    if not isinstance(role_claim, str) or not role_claim.strip():
        role_claim = "student"
    role = _ROLE_ALIASES.get(role_claim.lower(), PrincipalType.STUDENT) if isinstance(role_claim, str) else PrincipalType.STUDENT
    return Principal(
        principal_id=subject.strip(),
        principal_type=role,
        capabilities=_capabilities(claims, role),
        scopes=_scopes(claims),
        authenticated=True,
        consent_verified=_truthy_claim(claims, "guru_parental_consent", "parental_consent", "consent_verified"),
        revoked=_truthy_claim(claims, "guru_revoked", "revoked", "account_revoked"),
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
