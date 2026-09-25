"""ToolGateway: authenticate, authorize, validate, approve, execute, minimize, audit.

The language model never calls a handler. It proposes a tool name and
arguments; this gateway decides whether that call happens and what comes
back. Every invocation leaves an audit event, whatever its outcome.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import uuid4

from ..data_access.field_policy import strip_student_contact
from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.principals import Principal
from ..institution_data.store import InstitutionDataStore
from ..observability.tracing import TraceRecorder
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.authorization import DenialReason, authorize
from ..policy.pdp import LocalPolicyDecisionPoint, PolicyDecision, PolicyDecisionPoint
from .registry import PlatformToolRegistry
from .spec import PlatformToolSpec, RiskLevel, ToolArgumentError, ToolCallContext, ToolOutput

ControlStore = InMemoryControlStore | PostgresControlStore | SqliteControlStore
DEFAULT_APPROVAL_TTL_SECONDS = 900
HANDLER_FAILURE_MESSAGE = "the tool could not complete because of an internal error; the details have been logged for the administrator"
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolInvocation:
    tool_name: str
    status: str  # success | denied | invalid_arguments | approval_required | failed | unknown_tool
    data: Any = None
    summary: str = ""
    warnings: list[dict[str, str]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    records_returned: int = 0
    denial_reason: str | None = None
    approval: dict[str, Any] | None = None
    duration_ms: int = 0
    decision_id: str | None = None
    risk: str = RiskLevel.READ.value

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def as_dict(self, *, include_data: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": self.tool_name, "status": self.status, "summary": self.summary, "warnings": list(self.warnings),
            "provenance": list(self.provenance), "artifacts": list(self.artifacts), "records_returned": self.records_returned,
            "denial_reason": self.denial_reason, "approval": self.approval, "duration_ms": self.duration_ms, "risk": self.risk,
        }
        if include_data:
            payload["data"] = self.data
        return payload


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ToolGateway:
    registry: PlatformToolRegistry
    store: InstitutionDataStore
    control_store: ControlStore
    pdp: PolicyDecisionPoint = field(default_factory=LocalPolicyDecisionPoint)
    tracer: TraceRecorder = field(default_factory=TraceRecorder)
    approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS

    # ---------------------------------------------------------------- audit
    def _audit(self, context: ToolCallContext, tool_name: str, outcome: AuditOutcome, *, started: float, decision: PolicyDecision | None = None, extra: Mapping[str, str | int | bool | None] | None = None) -> None:
        metadata: list[tuple[str, str | int | bool | None]] = [("policy_version", self.pdp.policy_version), ("channel", context.channel)]
        if decision is not None:
            metadata.append(("pdp_decision_id", decision.decision_id))
        for key, value in (extra or {}).items():
            metadata.append((key, value))
        self.control_store.append_audit(AuditEvent(
            event_id=f"audit-{uuid4().hex}", event_type="gateway.tool", request_id=context.request_id,
            principal_id=context.principal.principal_id if context.principal.authenticated else None, endpoint="tool_gateway",
            conversation_id=context.conversation_id, source_ids=(context.institution_id,), tool_names=(tool_name,), outcome=outcome,
            redactions_applied=("student_contact_fields_policy", "arguments_not_persisted"), decision_metadata=tuple(metadata),
            duration_ms=int((monotonic() - started) * 1000),
        ))

    # ------------------------------------------------------------- approvals
    def _approval_state(self, context: ToolCallContext, tool: PlatformToolSpec, arguments: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """The matching approved approval record, or why the one the caller named cannot be used."""

        if not context.approval_id:
            return None, None
        record = self.store.get_approval(context.institution_id, context.approval_id)
        if record is None or record["principal_id"] != context.principal.principal_id:
            return None, "the earlier confirmation was not found"
        if record["tool_name"] != tool.name or record["arguments_sha256"] != arguments_digest(arguments):
            return None, "the earlier confirmation was for a different change"
        if record["status"] != "approved":
            return None, _UNUSABLE_APPROVAL.get(record["status"], f"the earlier confirmation is {record['status']}")
        if datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
            return None, _UNUSABLE_APPROVAL["expired"]
        return record, None

    def _release_approval(self, context: ToolCallContext, approval: Mapping[str, Any] | None) -> None:
        """Return a claimed approval to ``approved`` after the handler failed, so a retry needs no new confirmation."""

        if approval is not None:
            self.store.decide_approval(context.institution_id, approval["approval_id"], status="approved", decided_by=context.principal.principal_id)

    def request_approval(self, context: ToolCallContext, tool: PlatformToolSpec, arguments: Mapping[str, Any], reason: str, *, note: str | None = None) -> dict[str, Any]:
        """Ask for a confirmation; ``note`` says why an earlier one could not be used."""

        approval_id = f"apr-{uuid4().hex}"
        record = self.store.create_approval(
            context.institution_id, approval_id=approval_id, principal_id=context.principal.principal_id, tool_name=tool.name,
            arguments=dict(arguments), arguments_sha256=arguments_digest(arguments), reason=reason, ttl_seconds=self.approval_ttl_seconds,
        )
        return {"approval_id": approval_id, "tool_name": tool.name, "status": record.get("status"), "expires_at": record.get("expires_at"), "reason": reason, "arguments": dict(arguments), "note": note}

    def decide_approval(self, principal: Principal, institution_id: str, approval_id: str, *, approve: bool) -> dict[str, Any]:
        record = self.store.get_approval(institution_id, approval_id)
        if record is None:
            raise KeyError(f"approval not found: {approval_id}")
        if record["principal_id"] != principal.principal_id:
            raise PermissionError("only the requesting user can decide this approval")
        if record["status"] != "pending":
            raise ValueError(f"approval is already {record['status']}")
        if approve and datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
            # Accepting it would only have the re-sent command ask again, without saying why.
            self.store.decide_approval(institution_id, approval_id, status="expired", decided_by=principal.principal_id)
            raise ValueError("this confirmation has expired; send the command again to get a new one")
        decided = self.store.decide_approval(institution_id, approval_id, status="approved" if approve else "rejected", decided_by=principal.principal_id)
        return decided or record

    # ---------------------------------------------------------------- invoke
    async def invoke(self, tool_name: str, arguments: Mapping[str, Any] | None, context: ToolCallContext) -> ToolInvocation:
        started = monotonic()
        if not self.registry.has(tool_name):
            self._audit(context, tool_name, AuditOutcome.DENIED, started=started, extra={"reason": "unknown_tool"})
            return ToolInvocation(tool_name, "unknown_tool", denial_reason="the requested tool is not registered")
        tool = self.registry.get(tool_name)
        try:
            validated = tool.validate_arguments(arguments)
        except ToolArgumentError as exc:
            self._audit(context, tool_name, AuditOutcome.DENIED, started=started, extra={"reason": "invalid_arguments"})
            return ToolInvocation(tool_name, "invalid_arguments", denial_reason=str(exc), risk=tool.risk.value)
        decision_local = authorize(context.principal, tool.required_capability, context.scope)
        if not decision_local.allowed:
            reason = decision_local.reason.value if decision_local.reason else DenialReason.MISSING_CAPABILITY.value
            self._audit(context, tool_name, AuditOutcome.DENIED, started=started, extra={"reason": reason})
            return ToolInvocation(tool_name, "denied", denial_reason=_explain(reason, tool), risk=tool.risk.value)
        decision = self.pdp.evaluate(
            principal=context.principal, required_capability=tool.required_capability, action=tool.pdp_action,
            resource_type="platform_tool", resource_id=tool.name, requested_scope=context.scope,
        )
        if not decision.allowed:
            reason = decision.reason.value if isinstance(decision.reason, DenialReason) else str(decision.reason or "pdp_denied")
            self._audit(context, tool_name, AuditOutcome.DENIED, started=started, decision=decision, extra={"reason": reason})
            return ToolInvocation(tool_name, "denied", denial_reason=_explain(reason, tool), decision_id=decision.decision_id, risk=tool.risk.value)
        if tool.validator is not None:
            # Semantic validation (field names, value types, formats) runs before
            # any approval exists, so a confirmed action can never be burned on
            # arguments the handler would reject anyway.
            try:
                validated = tool.validator(context, validated)
            except PermissionError as exc:
                self._audit(context, tool_name, AuditOutcome.DENIED, started=started, decision=decision, extra={"reason": "handler_denied"})
                return ToolInvocation(tool_name, "denied", denial_reason=str(exc), decision_id=decision.decision_id, risk=tool.risk.value)
            except (ValueError, KeyError, LookupError) as exc:
                self._audit(context, tool_name, AuditOutcome.DENIED, started=started, decision=decision, extra={"reason": "invalid_arguments"})
                return ToolInvocation(tool_name, "invalid_arguments", denial_reason=str(exc)[:500], decision_id=decision.decision_id, risk=tool.risk.value)
            except Exception:  # noqa: BLE001 - a validator may consult the store; its failures are audited like a handler's
                logger.exception("tool validator failed: tool=%s request_id=%s principal=%s", tool.name, context.request_id, context.principal.principal_id)
                self._audit(context, tool_name, AuditOutcome.FAILED, started=started, decision=decision, extra={"reason": "handler_exception"})
                return ToolInvocation(tool_name, "failed", denial_reason=HANDLER_FAILURE_MESSAGE, decision_id=decision.decision_id, risk=tool.risk.value)
        approval: dict[str, Any] | None = None
        if tool.risk is RiskLevel.HIGH_RISK:
            approval, unusable = self._approval_state(context, tool, validated)
            # Claiming is atomic (status approved -> executing), so concurrent calls
            # carrying the same approval id cannot both run the handler; the loser
            # sees no usable approval and is asked to confirm again.
            if approval is not None:
                approval = self.store.claim_approval(context.institution_id, approval["approval_id"], principal_id=context.principal.principal_id)
                unusable = None if approval is not None else _UNUSABLE_APPROVAL["executing"]
            if approval is None:
                pending = self.request_approval(context, tool, validated, reason=f"{tool.name} changes institutional records and needs your confirmation", note=unusable)
                self._audit(context, tool_name, AuditOutcome.PARTIAL, started=started, decision=decision, extra={"reason": "approval_required", "approval_id": pending["approval_id"]})
                return ToolInvocation(tool_name, "approval_required", summary=pending["reason"], approval=pending, decision_id=decision.decision_id, risk=tool.risk.value)
        try:
            output = await tool.handler(context, validated)
        except (PermissionError,) as exc:
            self._release_approval(context, approval)
            self._audit(context, tool_name, AuditOutcome.DENIED, started=started, decision=decision, extra={"reason": "handler_denied"})
            return ToolInvocation(tool_name, "denied", denial_reason=str(exc), decision_id=decision.decision_id, risk=tool.risk.value)
        except (ValueError, KeyError, LookupError) as exc:
            self._release_approval(context, approval)
            self._audit(context, tool_name, AuditOutcome.FAILED, started=started, decision=decision, extra={"reason": "handler_error"})
            return ToolInvocation(tool_name, "failed", denial_reason=str(exc)[:500], decision_id=decision.decision_id, risk=tool.risk.value)
        except Exception:  # noqa: BLE001 - every invocation must leave an audit event, whatever the handler raised
            # Database, object-store and provider errors carry connection details
            # and raw SQL; they go to the log, never to the caller. The approval
            # (if any) goes back to approved so the user does not have to confirm again.
            logger.exception("tool handler failed: tool=%s request_id=%s principal=%s", tool.name, context.request_id, context.principal.principal_id)
            self._release_approval(context, approval)
            self._audit(context, tool_name, AuditOutcome.FAILED, started=started, decision=decision, extra={"reason": "handler_exception"})
            return ToolInvocation(tool_name, "failed", denial_reason=HANDLER_FAILURE_MESSAGE, decision_id=decision.decision_id, risk=tool.risk.value)
        if approval is not None:
            # A high-risk approval is single use: the completed action consumes it.
            self.store.decide_approval(context.institution_id, approval["approval_id"], status="consumed", decided_by=context.principal.principal_id)
        if not isinstance(output, ToolOutput):
            output = ToolOutput(data=output)
        data = strip_student_contact(output.data, context.principal)
        duration_ms = int((monotonic() - started) * 1000)
        self.tracer.record("gateway.tool", trace_id=context.request_id, attributes={
            "request_id": context.request_id, "principal_id": context.principal.principal_id, "tool_name": tool.name,
            "resource_type": "platform_tool", "action": tool.pdp_action, "status": "success", "latency_ms": duration_ms,
            "rows_used": output.records_returned, "decision_id": decision.decision_id, "policy_version": self.pdp.policy_version,
        })
        self._audit(context, tool_name, AuditOutcome.SUCCESS, started=started, decision=decision, extra={"risk": tool.risk.value, "records_returned": output.records_returned})
        return ToolInvocation(
            tool_name, "success", data=data, summary=output.summary, warnings=list(output.warnings), provenance=list(output.provenance),
            artifacts=list(output.artifacts), records_returned=output.records_returned, duration_ms=duration_ms, decision_id=decision.decision_id, risk=tool.risk.value,
        )


_UNUSABLE_APPROVAL = {
    "pending": "the earlier request was not confirmed",
    "rejected": "the earlier request was cancelled",
    "executing": "the earlier confirmation is already running",
    "consumed": "the earlier confirmation was already used",
    "expired": "the earlier confirmation expired",
}


def _explain(reason: str, tool: PlatformToolSpec) -> str:
    if reason == DenialReason.MISSING_CAPABILITY.value:
        return f"{tool.name} requires the {tool.required_capability.value} permission"
    if reason == DenialReason.OUT_OF_SCOPE.value:
        return "the requested institution is outside your authorised scope"
    if reason == DenialReason.UNAUTHENTICATED.value:
        return "authentication is required"
    if reason == DenialReason.REVOKED.value:
        return "this account has been revoked"
    if reason == DenialReason.PARENTAL_CONSENT_REQUIRED.value:
        return "verified consent is required for batch-level data"
    return f"policy denied {tool.name} ({reason})"


__all__ = ["DEFAULT_APPROVAL_TTL_SECONDS", "HANDLER_FAILURE_MESSAGE", "ToolGateway", "ToolInvocation", "arguments_digest"]
