"""Deployment-managed definitions for remote institutional connectors.

The API keeps one connector contract while allowing each institution to have
its own source identity, endpoint, approved semantic tools, and server-side
secret reference. Secrets are resolved from environment variables rather than
being embedded in the connector registry JSON.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse


DEFAULT_INSTITUTION_TOOLS = (
    "institution.overview",
    "institution.attendance_summary",
    "institution.source_health",
)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


@dataclass(frozen=True, slots=True)
class InstitutionConnectorDefinition:
    """One approved remote connector for one institutional source."""

    source_id: str
    institution_id: str
    display_name: str
    base_url: str
    allowed_tools: tuple[str, ...] = DEFAULT_INSTITUTION_TOOLS
    auth_token: str | None = field(default=None, repr=False)
    scope_attestation_required: bool = False

    def __post_init__(self) -> None:
        for field_name in ("source_id", "institution_id", "display_name", "base_url"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))

        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("institution connector base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "institution connector base URL must not contain credentials, query, or fragment data"
            )

        tools = tuple(_text(item, "allowed_tool") for item in self.allowed_tools)
        if not tools:
            raise ValueError("institution connector must declare at least one approved tool")
        if len(set(tools)) != len(tools):
            raise ValueError("institution connector allowed_tools must be unique")
        unknown = set(tools) - set(DEFAULT_INSTITUTION_TOOLS)
        if unknown:
            raise ValueError(f"institution connector contains unsupported tools: {sorted(unknown)}")
        object.__setattr__(self, "allowed_tools", tools)

        if self.auth_token is not None:
            object.__setattr__(self, "auth_token", _text(self.auth_token, "auth_token"))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> "InstitutionConnectorDefinition":
        """Build a definition from deployment JSON without embedding secrets."""

        if not isinstance(value, Mapping):
            raise ValueError("each institution connector must be an object")
        allowed_tools = value.get("allowed_tools", DEFAULT_INSTITUTION_TOOLS)
        if not isinstance(allowed_tools, list | tuple):
            raise ValueError("institution connector allowed_tools must be a list")
        auth_token_env = value.get("auth_token_env")
        if auth_token_env is not None:
            auth_token_env = _text(auth_token_env, "auth_token_env")
            token_source = environment if environment is not None else os.environ
            auth_token = token_source.get(auth_token_env) or None
        else:
            auth_token = None

        return cls(
            source_id=_text(value.get("source_id", ""), "source_id"),
            institution_id=_text(value.get("institution_id", ""), "institution_id"),
            display_name=_text(value.get("display_name", ""), "display_name"),
            base_url=_text(value.get("base_url", ""), "base_url"),
            allowed_tools=tuple(_text(item, "allowed_tool") for item in allowed_tools),
            auth_token=auth_token,
            scope_attestation_required=bool(value.get("scope_attestation_required", False)),
        )


def parse_institution_connectors(
    raw: str | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[InstitutionConnectorDefinition, ...]:
    """Parse ``GURU_INSTITUTION_CONNECTORS`` JSON into unique definitions."""

    if not raw or not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GURU_INSTITUTION_CONNECTORS must be a JSON array") from exc
    if not isinstance(value, list):
        raise ValueError("GURU_INSTITUTION_CONNECTORS must be a JSON array")

    definitions = tuple(
        InstitutionConnectorDefinition.from_mapping(item, environment=environment) for item in value
    )
    source_ids = [item.source_id for item in definitions]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("GURU_INSTITUTION_CONNECTORS source_id values must be unique")
    return definitions


__all__ = [
    "DEFAULT_INSTITUTION_TOOLS",
    "InstitutionConnectorDefinition",
    "parse_institution_connectors",
]
