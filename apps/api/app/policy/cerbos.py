"""HTTP Cerbos PDP adapter.

Agentic Saffron keeps the policy decision point behind its own narrow protocol. This
adapter accepts the Cerbos HTTP check contract, validates the response instead
of trusting an allow bit, and denies on timeout, malformed decisions, missing
policy metadata, or stale policy status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from .authorization import DenialReason
from .pdp import PolicyDecision, PolicyDecisionPoint


def _scope_payload(scope: InstitutionScope) -> dict[str, str | None]:
    return scope.as_dict()


def _scope_from_payload(value: object) -> InstitutionScope | None:
    if not isinstance(value, dict) or not isinstance(value.get("college_id"), str):
        return None
    try:
        return InstitutionScope(
            college_id=value["college_id"],
            department_id=value.get("department_id") if isinstance(value.get("department_id"), str) else None,
            batch_id=value.get("batch_id") if isinstance(value.get("batch_id"), str) else None,
        )
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CerbosPolicyDecisionPoint(PolicyDecisionPoint):
    """Strict Cerbos HTTP check client; no response is treated as implicit allow."""

    base_url: str
    policy_version: str
    timeout_seconds: float = 1.5
    require_fresh: bool = True
    transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Cerbos URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Cerbos URL must not contain credentials, query, or fragment data")
        if not self.policy_version.strip():
            raise ValueError("Cerbos policy version must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("Cerbos timeout must be positive")

    @property
    def _root(self) -> str:
        return self.base_url.rstrip("/")

    @staticmethod
    def _decision_id(payload: dict[str, Any]) -> str:
        value = payload.get("callId") or payload.get("decisionId") or payload.get("requestId")
        return str(value).strip() if isinstance(value, str) and value.strip() else f"pdp-{uuid4().hex}"

    def _denied(
        self,
        *,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
        reason: DenialReason | str,
        decision_id: str | None = None,
        policy_version: str | None = None,
        matched_scope: InstitutionScope | None = None,
    ) -> PolicyDecision:
        del principal, requested_scope
        return PolicyDecision(
            allowed=False,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            policy_version=policy_version or self.policy_version,
            decision_id=decision_id or f"pdp-{uuid4().hex}",
            matched_scope=matched_scope,
            reason=reason,
        )

    def _request_payload(
        self,
        *,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
        required_capability: Capability | None = None,
    ) -> dict[str, object]:
        resource_attributes: dict[str, object] = {"institution_scope": _scope_payload(requested_scope)}
        if required_capability is not None:
            resource_attributes["required_capability"] = required_capability.value
        return {
            "requestId": f"saffron-{uuid4().hex}",
            "principal": {
                "id": principal.principal_id,
                "roles": [principal.principal_type.value],
                "attr": {
                    "capabilities": sorted(item.value for item in principal.capabilities),
                    "institution_scopes": [_scope_payload(item) for item in principal.scopes],
                    "consent_verified": principal.consent_verified,
                    "revoked": principal.revoked,
                },
            },
            "resourceInstances": {
                resource_id: {
                    "resource": {
                        "id": resource_id,
                        "kind": resource_type,
                        "attr": resource_attributes,
                    },
                    "actions": [action],
                },
            },
        }

    @staticmethod
    def _extract_resource(payload: dict[str, Any], resource_id: str) -> dict[str, Any] | None:
        instances = payload.get("resourceInstances")
        if not isinstance(instances, dict):
            return None
        item = instances.get(resource_id)
        return item if isinstance(item, dict) else None

    def _parse_response(
        self,
        payload: dict[str, Any],
        *,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
    ) -> PolicyDecision:
        decision_id = self._decision_id(payload)
        raw_policy_version = payload.get("policyVersion") or payload.get("policy_version")
        response_version = str(raw_policy_version).strip() if isinstance(raw_policy_version, str) else ""
        if not response_version:
            if self.require_fresh:
                return self._denied(
                    principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                    requested_scope=requested_scope, reason=DenialReason.STALE_POLICY,
                    decision_id=decision_id,
                )
            # Standard Cerbos check responses do not echo a policy version. In
            # this explicitly less-strict mode, the pinned sidecar policy is
            # the version source; gateways that provide an attestation still
            # must match the configured version below.
            response_version = self.policy_version
        if response_version != self.policy_version:
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.STALE_POLICY,
                decision_id=decision_id, policy_version=response_version,
            )
        status = str(payload.get("policyStatus", payload.get("status", "current"))).strip().lower()
        if self.require_fresh and (status in {"stale", "unknown", "unavailable"} or payload.get("stale") is True):
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.STALE_POLICY,
                decision_id=decision_id,
            )
        resource = self._extract_resource(payload, resource_id)
        if resource is None:
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.PDP_UNAVAILABLE,
                decision_id=decision_id,
            )
        raw_actions = resource.get("actions")
        effect: object = None
        reason: str | None = None
        matched_scope: InstitutionScope | None = None
        if isinstance(raw_actions, dict):
            raw_effect = raw_actions.get(action)
            if isinstance(raw_effect, dict):
                effect = raw_effect.get("effect") or raw_effect.get("decision") or raw_effect.get("outcome")
                reason_value = raw_effect.get("reason")
                reason = reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
                matched_scope = _scope_from_payload(raw_effect.get("matchedScope") or raw_effect.get("matched_scope"))
            else:
                effect = raw_effect
        elif isinstance(raw_actions, list):
            # Some Cerbos-compatible gateways return a list of action results.
            for item in raw_actions:
                if isinstance(item, dict) and item.get("action") == action:
                    effect = item.get("effect") or item.get("decision")
                    reason_value = item.get("reason")
                    reason = reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
                    matched_scope = _scope_from_payload(item.get("matchedScope") or item.get("matched_scope"))
                    break
        allowed = str(effect).upper() in {"EFFECT_ALLOW", "ALLOW", "ALLOWED"}
        if allowed:
            if matched_scope is None:
                # Cerbos itself has already evaluated the scoped resource
                # attributes. The local principal/scope checks above prevent
                # this fallback from widening the requested scope.
                matched_scope = requested_scope
            if not matched_scope.covers(requested_scope):
                return self._denied(
                    principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                    requested_scope=requested_scope, reason=DenialReason.OUT_OF_SCOPE,
                    decision_id=decision_id, policy_version=response_version,
                )
            if (
                principal.principal_type is PrincipalType.STUDENT
                and requested_scope.batch_id is not None
                and not principal.consent_verified
            ):
                return self._denied(
                    principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                    requested_scope=requested_scope, reason=DenialReason.PARENTAL_CONSENT_REQUIRED,
                    decision_id=decision_id, policy_version=response_version,
                )
            return PolicyDecision(
                allowed=True,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                policy_version=response_version,
                decision_id=decision_id,
                matched_scope=matched_scope,
            )
        return self._denied(
            principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
            requested_scope=requested_scope, reason=reason or "pdp_denied",
            decision_id=decision_id, policy_version=response_version,
        )

    def evaluate(
        self,
        *,
        principal: Principal,
        required_capability: Capability,
        action: str,
        resource_type: str,
        resource_id: str,
        requested_scope: InstitutionScope,
    ) -> PolicyDecision:
        decision_id = f"pdp-{uuid4().hex}"
        if principal.revoked:
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.REVOKED, decision_id=decision_id,
            )
        if not principal.active:
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.UNAUTHENTICATED, decision_id=decision_id,
            )
        if not principal.has_capability(required_capability):
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.MISSING_CAPABILITY, decision_id=decision_id,
            )
        if action not in {"list", "search", "retrieve", "mcp.tool.call", "execute", "write", "high_risk"}:
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.UNKNOWN_ACTION, decision_id=decision_id,
            )
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                response = client.post(
                    f"{self._root}/api/check/resources",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    content=json.dumps(self._request_payload(
                        principal=principal,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        requested_scope=requested_scope,
                        required_capability=required_capability,
                    )),
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Cerbos response must be an object")
            return self._parse_response(
                payload,
                principal=principal,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                requested_scope=requested_scope,
            )
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            return self._denied(
                principal=principal, action=action, resource_type=resource_type, resource_id=resource_id,
                requested_scope=requested_scope, reason=DenialReason.PDP_UNAVAILABLE,
                decision_id=decision_id,
            )


__all__ = ["CerbosPolicyDecisionPoint"]
