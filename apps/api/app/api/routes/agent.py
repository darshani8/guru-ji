"""Master agent command routes: text or voice-transcript commands, approvals, tools."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...agents.contracts import MAX_COMMAND_CHARS, AgentCommand
from ...conversation.contracts import MAX_HISTORY_TURN_CHARS, MAX_HISTORY_TURNS, Turn
from ...domain.principals import Capability, InstitutionScope
from ..dependencies import platform_from_request, runtime_from_request
from ._platform_common import request_id_for, require_principal, resolve_institution, translate

router = APIRouter(prefix="/v1/agent", tags=["agent"])


class HistoryTurnBody(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_HISTORY_TURN_CHARS * 4)


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    institution_id: str | None = Field(default=None, max_length=128)
    department_id: str | None = Field(default=None, max_length=128)
    channel: str = Field(default="text", pattern="^(text|voice)$")
    conversation_id: str | None = Field(default=None, max_length=128)
    approval_id: str | None = Field(default=None, max_length=128)
    run_in_background: bool = False
    include_data: bool = True
    # The conversation so far, kept by the client; the server stores none.
    history: list[HistoryTurnBody] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS * 2)
    # The language the person chose; the reply follows the language they used.
    language: Literal["en-IN", "hi-IN", "kn-IN"] | None = None


class ApprovalDecisionBody(BaseModel):
    approve: bool


@router.post("/commands", summary="Run an institutional command through the master agent")
async def run_command(body: CommandBody, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.AGENT_COMMAND)
    target = resolve_institution(principal, body.institution_id)
    scope = InstitutionScope(target, body.department_id)
    if not principal.can_access(scope):
        raise HTTPException(status_code=403, detail="the requested department is outside the authenticated scope")
    dialogue = runtime_from_request(request).dialogue
    try:
        if dialogue is None:
            command = AgentCommand(request_id_for(request), principal, scope, body.command, body.channel, body.conversation_id, body.approval_id, body.run_in_background)
        else:
            turn = Turn(
                request_id_for(request), principal, scope, body.command, channel="voice" if body.channel == "voice" else "text", mode="agent",
                language_hint=body.language, history=tuple(item.model_dump() for item in body.history),  # type: ignore[arg-type]
                conversation_id=body.conversation_id, approval_id=body.approval_id, run_in_background=body.run_in_background,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if dialogue is None:
        return (await platform.agent.handle(command)).as_dict(include_data=body.include_data)
    reply = await dialogue.respond(turn)
    return reply.as_dict(include_data=body.include_data)


@router.get("/tools", summary="Tools available to the caller through the gateway")
async def list_tools(request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    return {"tools": platform.registry.describe(principal), "groups": platform.registry.groups()}


@router.get("/approvals", summary="Pending high-risk actions awaiting confirmation")
async def list_approvals(request: Request, institution_id: str | None = None, status: str | None = "pending") -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.AGENT_COMMAND)
    target = resolve_institution(principal, institution_id)
    return {"approvals": platform.store.list_approvals(target, principal_id=principal.principal_id, status=status or None)}


@router.post("/approvals/{approval_id}", summary="Confirm or reject a high-risk action")
async def decide_approval(approval_id: str, body: ApprovalDecisionBody, request: Request, institution_id: str | None = None) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.AGENT_COMMAND)
    target = resolve_institution(principal, institution_id)
    try:
        record = platform.gateway.decide_approval(principal, target, approval_id, approve=body.approve)
    except (PermissionError, KeyError, ValueError) as exc:
        raise translate(exc) from exc
    return {"approval": record, "next_step": "re-send the same command with approval_id to execute it" if body.approve else "the action was cancelled"}


@router.get("/runs", summary="Recent agent runs for the caller")
async def list_runs(request: Request, institution_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request, Capability.AGENT_COMMAND)
    target = resolve_institution(principal, institution_id)
    only_self = None if principal.has_capability(Capability.MANAGE_ACCESS) else principal.principal_id
    return {"runs": platform.store.list_agent_runs(target, principal_id=only_self, limit=min(max(limit, 1), 100))}


@router.get("/jobs/{job_id}", summary="Status of a background command or import")
async def job_status(job_id: str, request: Request) -> dict[str, Any]:
    platform = platform_from_request(request)
    principal = require_principal(request)
    job = platform.jobs.status(job_id)
    if job is None or not principal.can_access(InstitutionScope(str(job["institution_id"]))):
        raise HTTPException(status_code=404, detail="job not found")
    payload = job.get("payload") or {}
    requester = (payload.get("principal") or {}).get("principal_id") or payload.get("requested_by")
    if requester and requester != principal.principal_id and not principal.has_capability(Capability.MANAGE_ACCESS):
        raise HTTPException(status_code=403, detail="only the requester can view this job")
    return {"job": {key: job.get(key) for key in ("job_id", "institution_id", "job_type", "status", "created_at", "started_at", "finished_at", "result", "error", "attempts")}}


__all__ = ["router"]
