"""The master agent.

Understand -> plan -> check permission -> use tools -> perform work -> verify -> report.
Long commands can be accepted and executed in the background; the requester
is notified when the work completes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any
from uuid import uuid4

from ..data_access.service import InstitutionDataService
from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.principals import Capability, Principal
from ..gateway.gateway import ToolGateway
from ..gateway.registry import PlatformToolRegistry
from ..gateway.spec import ToolCallContext
from ..institution_data.store import InstitutionDataStore
from ..observability.tracing import TraceRecorder
from ..open_task.routing import OPEN_TASK_INTENT, route_to_open_task
from ..orchestration.answer_synthesizer import AssistantAnswer, apply_model_wording
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.query_limits import QueryLimits
from ..providers.model_base import TextModel
from .bindings import BindingError, resolve_arguments
from .contracts import AgentCommand, AgentPlan, AgentResponse, StepResult
from .planner import DeterministicPlanner, ModelPlanner, Vocabulary
from .specialists import SpecializedAgent, build_specialists
from .verification import overall_status, verify

ControlStore = InMemoryControlStore | PostgresControlStore | SqliteControlStore


@dataclass(slots=True)
class MasterAgent:
    gateway: ToolGateway
    registry: PlatformToolRegistry
    data: InstitutionDataService
    store: InstitutionDataStore
    control_store: ControlStore
    planner: DeterministicPlanner = field(default_factory=DeterministicPlanner)
    model_planner: ModelPlanner | None = None
    model: TextModel | None = None
    model_max_tokens: int = 800
    tracer: TraceRecorder = field(default_factory=TraceRecorder)
    limits: QueryLimits = field(default_factory=QueryLimits)
    specialists: dict[str, SpecializedAgent] = field(default_factory=dict)
    background: Any | None = None  # JobQueue; set by the runtime when background execution is enabled
    open_task: Any | None = None  # OpenTaskAgent, when GURU_OPEN_TASK_ENABLED

    def __post_init__(self) -> None:
        if not self.specialists:
            self.specialists = build_specialists(self.gateway)

    # --------------------------------------------------------------- helpers
    def _vocabulary(self, institution_id: str) -> Vocabulary:
        try:
            return Vocabulary(tuple(self.data.known_programs(institution_id)), tuple(self.data.known_departments(institution_id)))
        except Exception:  # noqa: BLE001 - vocabulary is an aid, never a blocker
            return Vocabulary()

    async def plan(self, command: AgentCommand) -> AgentPlan:
        tools = self.registry.for_principal(command.principal)
        vocabulary = self._vocabulary(command.scope.college_id)
        if self.model_planner is not None:
            return await self.model_planner.plan(command.text, tools, vocabulary)
        return self.planner.plan(command.text, tools, vocabulary)

    def _refuse(self, command: AgentCommand, reason: str, started: float) -> AgentResponse:
        response = AgentResponse(command.request_id, "refused", f"I cannot run this command: {reason}", refusal_reason=reason, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id)
        self._record(command, None, response, started)
        return response

    def _record(self, command: AgentCommand, plan: AgentPlan | None, response: AgentResponse, started: float) -> None:
        outcome = {"complete": AuditOutcome.SUCCESS, "partial": AuditOutcome.PARTIAL, "refused": AuditOutcome.DENIED, "accepted": AuditOutcome.SUCCESS}.get(response.status, AuditOutcome.FAILED)
        if response.status in {"needs_input", "approval_required"}:
            outcome = AuditOutcome.PARTIAL
        tool_names = tuple(step.tool for step in response.steps) or tuple(step.tool for step in (plan.steps if plan else []))
        self.control_store.append_audit(AuditEvent(
            event_id=f"audit-{uuid4().hex}", event_type="agent.command", request_id=command.request_id,
            principal_id=command.principal.principal_id if command.principal.authenticated else None, endpoint="/v1/agent/commands",
            conversation_id=command.conversation_id, source_ids=(command.scope.college_id,), tool_names=tool_names, outcome=outcome,
            redactions_applied=("command_text_hashed", "tool_data_not_persisted"),
            decision_metadata=(("intent", response.intent), ("planner", plan.planner if plan else "none"), ("channel", command.channel), ("status", response.status)),
            duration_ms=response.duration_ms,
        ))
        try:
            self.store.add_agent_run(
                command.scope.college_id, run_id=f"run-{uuid4().hex}", principal_id=command.principal.principal_id, request_id=command.request_id, channel=command.channel,
                command_sha256=hashlib.sha256(command.text.encode("utf-8")).hexdigest(), status=response.status, plan=[step.as_dict() for step in (plan.steps if plan else [])],
                steps=[step.as_dict(include_data=False) for step in response.steps], tool_names=list(tool_names), duration_ms=response.duration_ms,
            )
        except Exception:  # noqa: BLE001 - run history is best effort; the audit event above is the record of truth
            pass

    # --------------------------------------------------------------- execute
    async def execute(self, command: AgentCommand, plan: AgentPlan) -> list[StepResult]:
        results: list[StepResult] = []
        completed: dict[str, Any] = {}
        failed: set[str] = set()
        for step in plan.steps:
            if any(dependency in failed for dependency in step.depends_on):
                failed.add(step.step_id)
                continue
            try:
                arguments = resolve_arguments(step.arguments, step.bindings, completed)
            except BindingError as exc:
                results.append(StepResult(step.step_id, step.tool, "failed", summary=str(exc), denial_reason=str(exc)))
                failed.add(step.step_id)
                continue
            agent = self.specialists.get(step.agent) or self.specialists["data"]
            context = ToolCallContext(command.request_id, command.principal, command.scope, command.channel, self.limits, command.approval_id, command.conversation_id)
            result = await agent.run(step, arguments, context)
            results.append(result)
            if result.status == "approval_required":
                break
            if result.ok:
                completed[step.step_id] = {"data": result.data, "summary": result.summary}
            else:
                failed.add(step.step_id)
        return results

    # ---------------------------------------------------------------- report
    @staticmethod
    def _listing(result: StepResult) -> str | None:
        """Name the records when the user asked for names, keeping the list bounded."""

        data = result.data if isinstance(result.data, dict) else {}
        rows = data.get("students") or data.get("faculty") or data.get("hods") or data.get("staff")
        if not isinstance(rows, list) or not rows:
            return None
        parts: list[str] = []
        for row in rows[:25]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("name") or row.get("student_id") or row.get("faculty_id") or "")
            identifier = row.get("student_id") or row.get("faculty_id") or row.get("staff_id")
            detail = ""
            if row.get("attendance_percent") is not None:
                detail = f", {row['attendance_percent']}%"
            elif row.get("balance") is not None:
                detail = f", balance {row['balance']:,.2f}"
            elif row.get("designation"):
                detail = f", {row['designation']}"
            parts.append(f"{label} ({identifier}{detail})" if identifier and identifier != label else f"{label}{detail}")
        if not parts:
            return None
        suffix = f" and {len(rows) - 25} more" if len(rows) > 25 else ""
        return "Names: " + "; ".join(parts) + suffix + "."

    def _compose(self, plan: AgentPlan, results: list[StepResult], status: str) -> str:
        fragments: list[str] = []
        wants_list = bool(plan.entities.get("wants_list")) and not plan.entities.get("wants_count")
        for result in results:
            if result.ok and result.summary:
                fragments.append(result.summary)
                if wants_list:
                    listing = self._listing(result)
                    if listing:
                        fragments.append(listing)
            elif result.status == "approval_required":
                fragments.append(f"{result.tool} needs your confirmation before it runs.")
            elif result.status in {"denied", "failed", "invalid_arguments", "unknown_tool"}:
                fragments.append(f"{result.tool} could not run: {result.denial_reason or result.status}.")
        for result in results:
            for artifact in result.artifacts:
                if artifact.get("type") == "report":
                    fragments.append(f"Download: {artifact.get('download_path')}")
        if not fragments:
            fragments.append("No step produced a result.")
        if status == "partial":
            fragments.append("Some steps did not complete; the answer above covers only the successful ones.")
        return " ".join(fragments)

    async def handle(self, command: AgentCommand) -> AgentResponse:
        started = monotonic()
        principal: Principal = command.principal
        if not principal.active:
            return self._refuse(command, "authentication is required", started)
        if not principal.has_capability(Capability.AGENT_COMMAND):
            return self._refuse(command, "the agent:command permission is required", started)
        if not principal.can_access(command.scope):
            return self._refuse(command, "the requested institution is outside your authorised scope", started)
        self.tracer.record("agent.command", trace_id=command.request_id, attributes={"request_id": command.request_id, "principal_id": principal.principal_id, "scope_college_id": command.scope.college_id})
        plan = await self.plan(command)
        if self.open_task is not None:
            route = route_to_open_task(command.text, plan)
            if route is not None:
                return await self._open_task(command, route, started)
        if plan.clarification:
            response = AgentResponse(command.request_id, "needs_input", plan.clarification, intent=plan.intent, plan=plan.as_dict(), clarification=plan.clarification, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id)
            self._record(command, plan, response, started)
            return response
        if command.run_in_background and not command.in_background and self.background is not None:
            job_id = self._enqueue(command)
            response = AgentResponse(command.request_id, "accepted", "Understood. I am working on it in the background and will notify you when it is done.", intent=plan.intent, plan=plan.as_dict(), job_id=job_id, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id)
            self._record(command, plan, response, started)
            return response
        results = await self.execute(command, plan)
        status = overall_status(plan, results)
        warnings = [warning for result in results for warning in result.warnings] + verify(plan, results)
        answer_text = self._compose(plan, results, status)
        generation_mode = "deterministic"
        if status in {"complete", "partial"} and self.model is not None:
            worded = await apply_model_wording(AssistantAnswer(command.request_id, status, answer_text), self.model, max_tokens=self.model_max_tokens)
            answer_text = worded.answer
            generation_mode = worded.generation_mode
            warnings.extend(dict(item) for item in worded.warnings)
        approval = next((result.approval for result in results if result.status == "approval_required"), None)
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for result in results:
            for item in result.provenance:
                key = f"{item.get('source_id')}|{item.get('title', '')}"
                if key not in seen:
                    seen.add(key)
                    sources.append({"source_id": item.get("source_id"), "title": item.get("title") or f"{item.get('source_id')} ({item.get('source_type')})", "locator": item.get("locator") or item.get("retrieved_at", "retrieved"), **({"url": item["source_id"]} if str(item.get("source_type")) == "public_web" else {}), **({"published_at": item["published_at"]} if item.get("published_at") else {})})
        response = AgentResponse(
            command.request_id, status, answer_text, intent=plan.intent, plan=plan.as_dict(), steps=results, sources=sources, warnings=warnings,
            artifacts=[artifact for result in results for artifact in result.artifacts], approval=approval, generation_mode=generation_mode,
            duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id,
            refusal_reason=(results[0].denial_reason if status == "refused" and results else None),
        )
        self._record(command, plan, response, started)
        return response

    def _enqueue(self, command: AgentCommand) -> str:
        principal = command.principal
        return self.background.enqueue(command.scope.college_id, "agent.command", {  # type: ignore[union-attr]
            "request_id": command.request_id, "text": command.text, "channel": command.channel, "conversation_id": command.conversation_id,
            "principal": {"principal_id": principal.principal_id, "principal_type": principal.principal_type.value, "capabilities": sorted(item.value for item in principal.capabilities), "scopes": [scope.as_dict() for scope in principal.scopes], "consent_verified": principal.consent_verified},
            "scope": command.scope.as_dict(), "approval_id": command.approval_id,
        })

    async def _open_task(self, command: AgentCommand, route: str, started: float) -> AgentResponse:
        """Hand the command to the open-task agent: at once, or as a background job when a real queue runs them."""

        plan = AgentPlan(OPEN_TASK_INTENT, summary=f"open task ({route})", planner="open_task")
        refusal = self.open_task.refusal(command.principal)  # type: ignore[union-attr]
        unavailable = None if refusal else self.open_task.unavailable()  # type: ignore[union-attr]
        if refusal or unavailable:
            response = AgentResponse(
                command.request_id, "refused" if refusal else "failed", f"I cannot do this task: {refusal or unavailable}.", intent=OPEN_TASK_INTENT, plan=plan.as_dict(),
                refusal_reason=refusal, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id,
            )
            self._record(command, plan, response, started)
            return response
        queue_runs_later = self.background is not None and getattr(self.background, "backend_name", "inline") != "inline"
        if not command.in_background and self.background is not None and (command.run_in_background or (self.open_task.background and queue_runs_later)):  # type: ignore[union-attr]
            job_id = self._enqueue(command)
            response = AgentResponse(
                command.request_id, "accepted", "Understood. This needs the open-task agent, which is working on it in the background. You will get a notification with the files when it is done.",
                intent=OPEN_TASK_INTENT, plan=plan.as_dict(), job_id=job_id, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id,
            )
            self._record(command, plan, response, started)
            return response
        response = await self.open_task.run(command)  # type: ignore[union-attr]
        self._record(command, plan, response, started)
        return response

    def with_background(self, queue: Any) -> "MasterAgent":
        return replace(self, background=queue)


__all__ = ["MasterAgent"]
