"""The master agent.

Understand -> plan -> check permission -> use tools -> perform work -> verify -> report.
Long commands can be accepted and executed in the background; the requester
is notified when the work completes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any
from uuid import uuid4

from ..data_access.service import InstitutionDataService
from ..domain.audit import AuditEvent, AuditOutcome
from ..domain.principals import Capability, Principal
from ..gateway.gateway import ToolGateway
from ..gateway.registry import PlatformToolRegistry
from ..gateway.spec import RiskLevel, ToolCallContext
from ..institution_data.store import InstitutionDataStore
from ..observability.tracing import TraceRecorder
from ..open_task.routing import OPEN_TASK_INTENT, route_to_open_task
from ..orchestration.answer_synthesizer import AssistantAnswer, apply_model_wording
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..policy.query_limits import QueryLimits
from ..providers.model_base import TextModel
from .bindings import BindingError, referenced_steps, resolve_arguments
from .contracts import AgentCommand, AgentPlan, AgentResponse, PlanStep, StepResult
from .planner import TOOL_GROUP_AGENT, DeterministicPlanner, ModelPlanner, Vocabulary
from .specialists import SpecializedAgent, build_specialists
from .verification import overall_status, verify

ControlStore = InMemoryControlStore | PostgresControlStore | SqliteControlStore
ONE_CHANGE_PER_CONFIRMATION = "I can change one record per confirmation. Please ask for each change separately."
logger = logging.getLogger(__name__)


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
    open_task: Any | None = None  # OpenTaskAgent, when SAFFRON_OPEN_TASK_ENABLED

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

    def _risk(self, step: PlanStep) -> RiskLevel:
        return self.registry.get(step.tool).risk if self.registry.has(step.tool) else RiskLevel.HIGH_RISK

    def _approved_call(self, command: AgentCommand) -> Mapping[str, Any] | None:
        """The confirmed tool call a re-sent command carries, while it can still run."""

        if not command.approval_id:
            return None
        try:
            record = self.store.get_approval(command.scope.college_id, command.approval_id)
        except Exception:  # noqa: BLE001 - the gateway reports an unusable confirmation itself
            logger.exception("could not read approval %s", command.approval_id)
            return None
        if record is None or record.get("status") != "approved" or record.get("principal_id") != command.principal.principal_id or not self.registry.has(str(record.get("tool_name"))):
            return None
        return record

    def _resume(self, approved: Mapping[str, Any]) -> tuple[AgentPlan, dict[str, Any]]:
        """The plan a confirmed re-send runs, and the outputs of the steps that already ran.

        It is the plan that asked for the confirmation, kept with it, from the
        confirmed step on, with the confirmed call in place of that step:
        planning the words again can give other arguments (a model's wording,
        or its fallback) whose confirmation would be asked for without end,
        other steps, or another order. Steps before it ran when the
        confirmation was asked for; later ones use their kept outputs. A
        confirmation kept without its plan runs the confirmed call alone.
        """

        tool = self.registry.get(str(approved["tool_name"]))
        kept = approved.get("resume") or {}
        try:
            steps = [PlanStep(str(item["step_id"]), str(item["tool"]), dict(item.get("arguments") or {}), str(item.get("purpose") or ""), tuple(item.get("depends_on") or ()), dict(item.get("bindings") or {}), str(item.get("agent") or "data")) for item in kept["plan"]["steps"]]
            index = next(position for position, step in enumerate(steps) if step.step_id == kept["stopped_at"] and step.tool == tool.name)
        except (KeyError, TypeError, ValueError, StopIteration):
            confirmed = PlanStep("s1", tool.name, dict(approved["arguments"]), f"run the confirmed {tool.name}", agent=TOOL_GROUP_AGENT.get(tool.group, "data"))
            return AgentPlan(tool.name, [confirmed], summary=confirmed.purpose, planner="approval"), {}
        stopped = steps[index]
        remaining = [PlanStep(stopped.step_id, tool.name, dict(approved["arguments"]), stopped.purpose, (), {}, stopped.agent), *steps[index + 1:]]
        # Dependencies on steps that already ran are met by their kept outputs.
        present = {step.step_id for step in remaining}
        for step in remaining:
            step.depends_on = tuple(dependency for dependency in step.depends_on if dependency in present)
        plan = kept["plan"]
        resumed = AgentPlan(str(plan.get("intent") or tool.name), remaining, summary=" then ".join(step.purpose for step in remaining), planner="approval", entities=dict(plan.get("entities") or {}))
        return resumed, dict(kept.get("completed") or {})

    def _keep_for_resume(self, command: AgentCommand, plan: AgentPlan, results: list[StepResult]) -> None:
        """Keep, with the confirmation just asked for, the plan and the outputs its later steps use."""

        stopped = next((result for result in results if result.status == "approval_required" and result.approval), None)
        if stopped is None:
            return
        index = next(position for position, step in enumerate(plan.steps) if step.step_id == stopped.step_id)
        needed = {step_id for step in plan.steps[index + 1:] for step_id in referenced_steps(step)}
        outputs = {result.step_id: {"data": result.data, "summary": result.summary} for result in results if result.ok and result.step_id in needed}
        try:
            self.store.set_approval_resume(command.scope.college_id, str(stopped.approval["approval_id"]), principal_id=command.principal.principal_id, resume={"plan": plan.as_dict(), "stopped_at": stopped.step_id, "completed": outputs})
        except Exception:  # noqa: BLE001 - without it the confirmed call still runs, alone
            logger.exception("could not keep the plan for approval %s", stopped.approval.get("approval_id"))

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
    async def execute(self, command: AgentCommand, plan: AgentPlan, completed: Mapping[str, Any] | None = None) -> list[StepResult]:
        """Run the plan in order; ``completed`` holds the outputs of steps that ran earlier (a resumed plan)."""

        results: list[StepResult] = []
        completed = dict(completed or {})
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
                note = (result.approval or {}).get("note")
                fragments.append(f"{result.tool} needs your confirmation before it runs{f' ({note})' if note else ''}.")
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
        approved = self._approved_call(command)
        earlier: dict[str, Any] = {}
        if approved is not None:
            plan, earlier = self._resume(approved)
        else:
            plan = await self.plan(command)
        if approved is None and self.open_task is not None:
            route = route_to_open_task(command.text, plan)
            if route is not None:
                return await self._open_task(command, route, started)
        clarification = plan.clarification
        record_changes = sum(1 for step in plan.steps if self._risk(step) is RiskLevel.HIGH_RISK)
        if not clarification and record_changes > 1:
            # Execution stops at the first confirmation, so a second change could never run.
            clarification = ONE_CHANGE_PER_CONFIRMATION
        if clarification:
            response = AgentResponse(command.request_id, "needs_input", clarification, intent=plan.intent, plan=plan.as_dict(), clarification=clarification, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id)
            self._record(command, plan, response, started)
            return response
        # A record change runs in the foreground: its confirmation, and then its
        # result, must reach the person, and a background job has nowhere to ask.
        if command.run_in_background and not command.in_background and self.background is not None and not record_changes:
            job_id = self._enqueue(command)
            response = AgentResponse(command.request_id, "accepted", "Understood. I am working on it in the background and will notify you when it is done.", intent=plan.intent, plan=plan.as_dict(), job_id=job_id, duration_ms=int((monotonic() - started) * 1000), conversation_id=command.conversation_id)
            self._record(command, plan, response, started)
            return response
        results = await self.execute(command, plan, earlier)
        status = overall_status(plan, results)
        if status == "approval_required":
            self._keep_for_resume(command, plan, results)
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
