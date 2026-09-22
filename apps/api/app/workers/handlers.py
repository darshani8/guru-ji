"""Job handlers registered with the queue at runtime construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..actions.notifications import NotificationService
from ..agents.contracts import AgentCommand
from ..agents.master import MasterAgent
from ..domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from ..ingestion.service import IngestionService
from ..internet_intelligence.monitoring import ContinuousMonitor
from .queue import JobQueue


def principal_from_snapshot(snapshot: Mapping[str, Any]) -> Principal:
    capabilities = frozenset(Capability(item) for item in snapshot.get("capabilities", []) if item in Capability._value2member_map_)
    scopes = tuple(InstitutionScope(str(item["college_id"]), item.get("department_id"), item.get("batch_id")) for item in snapshot.get("scopes", []) if isinstance(item, Mapping) and item.get("college_id"))
    return Principal(str(snapshot["principal_id"]), PrincipalType(str(snapshot.get("principal_type", "student"))), capabilities, scopes, authenticated=True, consent_verified=bool(snapshot.get("consent_verified", False)))


def register_handlers(queue: JobQueue, *, ingestion: IngestionService | None = None, agent: MasterAgent | None = None, monitor: ContinuousMonitor | None = None, notifications: NotificationService | None = None, map_engine: Any | None = None, map_desk: Any | None = None) -> None:
    if ingestion is not None:
        async def process_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
            # A re-dispatched attempt means the queue established that the previous
            # worker died, so an interrupted run may be restarted even if its
            # heartbeat looks recent; an operator retry may also force it.
            force = bool(payload.get("force")) or int(payload.get("_attempt") or 1) > 1
            job = await ingestion.process(str(payload["institution_id"]), str(payload["job_id"]), force=force)
            if notifications is not None and payload.get("requested_by"):
                report = job.get("report", {}).get("import") or {}
                body = f"Import job {job['job_id']} finished with status {job['status']} ({job['stage']})."
                if report:
                    body += f" Inserted {report.get('inserted', 0)}, updated {report.get('updated', 0)}, unchanged {report.get('unchanged', 0)}, skipped {report.get('skipped', 0)}."
                if job["status"] == "needs_review":
                    body += " Review is required before the data is imported."
                notifications.system_notify(str(payload["institution_id"]), recipient_ids=[str(payload["requested_by"])], title="Data import update", body=body, reference_type="ingestion_job", reference_id=job["job_id"])
            return {"job_id": job["job_id"], "status": job["status"], "stage": job["stage"]}

        queue.register("ingestion.process", process_ingestion)

        def ingestion_abandoned(job: Mapping[str, Any], error: str) -> None:
            payload = job.get("payload") or {}
            if payload.get("institution_id") and payload.get("job_id"):
                ingestion.store.update_job(str(payload["institution_id"]), str(payload["job_id"]), status="failed", error=f"processing could not be scheduled: {error}"[:500])

        queue.register_failure_hook("ingestion.process", ingestion_abandoned)

    if agent is not None:
        async def run_command(payload: dict[str, Any]) -> dict[str, Any]:
            principal = principal_from_snapshot(payload["principal"])
            scope_payload = payload.get("scope") or {}
            scope = InstitutionScope(str(scope_payload.get("college_id")), scope_payload.get("department_id"), scope_payload.get("batch_id"))
            command = AgentCommand(str(payload["request_id"]), principal, scope, str(payload["text"]), str(payload.get("channel", "text")), payload.get("conversation_id"), payload.get("approval_id"), run_in_background=False)
            response = await agent.handle(command)
            if notifications is not None:
                body = response.answer[:3500]
                notifications.system_notify(scope.college_id, recipient_ids=[principal.principal_id], title=f"Command finished: {response.status}", body=body, reference_type="agent_run", reference_id=response.request_id)
            return {"status": response.status, "answer": response.answer[:2000], "artifacts": response.artifacts, "warnings": response.warnings[:20]}

        queue.register("agent.command", run_command)

    if monitor is not None:
        async def run_monitor(payload: dict[str, Any]) -> dict[str, Any]:
            if payload.get("institution_id"):
                return await monitor.run_for(str(payload["institution_id"]))
            return {"runs": await monitor.run_all()}

        queue.register("intelligence.monitor", run_monitor)

    if map_engine is not None:
        async def run_map_tick(payload: dict[str, Any]) -> dict[str, Any]:
            # A re-dispatched attempt resumes from the sources' own schedule and
            # leases; the per-institution run lock keeps two ticks from overlapping.
            return await map_engine.tick(str(payload["institution_id"]))

        queue.register("intelligence.map_tick", run_map_tick)

    if map_desk is not None:
        async def send_map_digest(payload: dict[str, Any]) -> dict[str, Any]:
            digest = map_desk.send_digest(str(payload["institution_id"]))
            return {"sent": digest["sent"], "incidents": len(digest["incidents"]), "found": digest["found"]}

        queue.register("intelligence.map_digest", send_map_digest)


__all__ = ["principal_from_snapshot", "register_handlers"]
