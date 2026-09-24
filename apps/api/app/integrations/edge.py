"""Trusted LMS edge adapters.

The browser never supplies Agentic Saffron roles or capabilities directly. An adapter
calls a configured, authenticated LMS edge endpoint and derives the principal
from its server response, preserving only the institution scope needed for
policy evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..auth.roles import ROLE_ALIASES, ROLE_CAPABILITIES
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType


_ROLE_TYPES = ROLE_ALIASES
# LMS edge sessions receive the shared role grant plus source metadata, which the
# LMS integration exposes to every authenticated member.
_CAPABILITIES_BY_ROLE = {
    role: grant | frozenset({Capability.VIEW_SOURCE_METADATA})
    for role, grant in ROLE_CAPABILITIES.items()
    if role is not PrincipalType.ANONYMOUS
}


@dataclass(frozen=True, slots=True)
class EdgeIdentityAdapter:
    base_url: str
    auth_token: str = field(repr=False)
    identity_path: str
    transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("edge adapter URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("edge adapter URL must not contain credentials, query, or fragment data")
        if not self.auth_token.strip():
            raise ValueError("edge adapter auth token must not be blank")
        if not self.identity_path.startswith("/"):
            raise ValueError("edge identity path must start with /")

    def resolve(self, edge_session_assertion: str) -> Principal:
        if not edge_session_assertion.strip():
            raise PermissionError("edge session assertion is required")
        try:
            with httpx.Client(timeout=3.0, transport=self.transport, follow_redirects=False) as client:
                response = client.get(
                    f"{self.base_url.rstrip('/')}{self.identity_path}",
                    headers={
                        "Authorization": f"Bearer {self.auth_token}",
                        "X-Guru-Edge-Session": edge_session_assertion,
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PermissionError("edge identity service unavailable or invalid") from exc
        if not isinstance(payload, dict):
            raise PermissionError("edge identity response was invalid")
        return self._principal_from_verified_claims(payload)

    @staticmethod
    def _principal_from_verified_claims(payload: dict[str, Any]) -> Principal:
        principal_id = payload.get("sub") or payload.get("user_id")
        role = payload.get("role")
        if not isinstance(principal_id, str) or not principal_id.strip() or not isinstance(role, str):
            raise PermissionError("edge identity response lacked verified subject or role")
        principal_type = _ROLE_TYPES.get(role.strip().lower())
        if principal_type is None:
            raise PermissionError("edge identity role is not supported")
        scope_value = payload.get("institution_scope")
        if not isinstance(scope_value, dict) or not isinstance(scope_value.get("college_id"), str):
            raise PermissionError("edge identity response lacked institution scope")
        scope = InstitutionScope(
            college_id=scope_value["college_id"],
            department_id=scope_value.get("department_id") if isinstance(scope_value.get("department_id"), str) else None,
            batch_id=scope_value.get("batch_id") if isinstance(scope_value.get("batch_id"), str) else None,
        )
        return Principal(
            principal_id=principal_id.strip(),
            principal_type=principal_type,
            capabilities=_CAPABILITIES_BY_ROLE[principal_type],
            scopes=(scope,),
            authenticated=True,
            consent_verified=bool(payload.get("consent_verified", False)),
            revoked=bool(payload.get("revoked", False)),
        )


@dataclass(frozen=True, slots=True)
class OpenEdxAdapter(EdgeIdentityAdapter):
    identity_path: str = "/api/guru/identity"


@dataclass(frozen=True, slots=True)
class MoodleAdapter(EdgeIdentityAdapter):
    identity_path: str = "/local/guru/identity"


__all__ = ["EdgeIdentityAdapter", "MoodleAdapter", "OpenEdxAdapter"]
