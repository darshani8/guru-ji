"""Job handlers registered with the queue at runtime construction."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..actions.notifications import NotificationService
from ..agents.contracts import AgentCommand
from ..agents.master import MasterAgent
from ..domain.principals import InstitutionScope, principal_from_snapshot
from ..ingestion.service import IngestionService
from ..internet_intelligence.monitoring import ContinuousMonitor
from .queue import JobQueue


def register_handlers(
    queue: JobQueue, *, ingestion: IngestionService | None = None, agent: MasterAgent | None = None, monitor: ContinuousMonitor | None = None, notifications: NotificationService | None = None, map_engine: Any | None = None,
    map_desk: Any | None = None, retention: Callable[..., Mapping[str, Any]] | None = None,
) -> None:
    """Register every job this process can run; ``retention(institution_id=...)`` applies the intelligence retention period."""

    if ingestion is not None:
        async def process_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
            # A re-dispatched attempt means the queue established that the previous
            # worker died, so an interrupted run may be restarted even if its
            # heartbeat looks recent; an operator retry may also force it. A step
            # the API recorded and handed over (``resume``: normalising under an
            # approved mapping, or importing) is this job's own work.
            force = bool(payload.get("force") or payload.get("resume")) or int(payload.get("_attempt") or 1) > 1
            job = await ingestion.process(str(payload["institution_id"]), str(payload["job_id"]), force=force)
            # Sheets of the workbook that became jobs of their own are queued
            # here, the same way in every queue mode; one the queue refuses is
            # marked failed with the reason, so it can be retried.
            for sibling in ingestion.queued_siblings(str(payload["institution_id"]), job):
                try:
                    queue.enqueue(str(payload["institution_id"]), "ingestion.process", {"institution_id": str(payload["institution_id"]), "job_id": sibling["job_id"], "requested_by": sibling["requested_by"]})
                except RuntimeError as exc:
                    ingestion.record_unscheduled(str(payload["institution_id"]), sibling["job_id"], f"processing could not be scheduled: {exc}")
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
                ingestion.record_unscheduled(str(payload["institution_id"]), str(payload["job_id"]), f"processing could not be scheduled: {error}")

        queue.register_failure_hook("ingestion.process", ingestion_abandoned)

    if agent is not None:
        async def run_command(payload: dict[str, Any]) -> dict[str, Any]:
            principal = principal_from_snapshot(payload["principal"])
            scope_payload = payload.get("scope") or {}
            scope = InstitutionScope(str(scope_payload.get("college_id")), scope_payload.get("department_id"), scope_payload.get("batch_id"))
            command = AgentCommand(str(payload["request_id"]), principal, scope, str(payload["text"]), str(payload.get("channel", "text")), payload.get("conversation_id"), payload.get("approval_id"), run_in_background=False, in_background=True)
            response = await agent.handle(command)
            if notifications is not None:
                body = response.answer[:3500]
                notifications.system_notify(scope.college_id, recipient_ids=[principal.principal_id], title=f"Command finished: {response.status}", body=body, reference_type="agent_run", reference_id=response.request_id)
            return {"status": response.status, "answer": response.answer[:2000], "artifacts": response.artifacts, "warnings": response.warnings[:20], "approval": response.approval}

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
            # The job row keeps counts only: review items and incidents live in
            # their own manager-only stores, where a redaction reaches them.
            result = await map_engine.tick(str(payload["institution_id"]))
            return {key: result.get(key) for key in ("institution_id", "run_id", "skipped", "stop_reason", "counts", "spend", "metrics") if key in result}

        queue.register("intelligence.map_tick", run_map_tick)

    if map_desk is not None:
        async def send_map_digest(payload: dict[str, Any]) -> dict[str, Any]:
            institution_id = str(payload["institution_id"])
            digest = map_desk.send_digest(institution_id)
            map_desk.store.cache_prune()  # the shared public-web cache: drop what expired, once a day
            result: dict[str, Any] = {"sent": digest["sent"], "incidents": len(digest["incidents"]), "found": digest["found"]}
            if retention is not None:
                # The digest is the map's once-a-day job, so the retention period is applied with
                # it. A failure is reported, not raised: a retried job would send the digest twice.
                try:
                    result["pruned"] = retention(institution_id=institution_id)
                except Exception as exc:  # noqa: BLE001 - housekeeping must not fail a digest already sent
                    logging.getLogger(__name__).warning("intelligence retention pruning failed for %s: %s", institution_id, exc)
                    result["pruned"] = {"error": str(exc)[:300]}
            return result

        queue.register("intelligence.map_digest", send_map_digest)


__all__ = ["principal_from_snapshot", "register_handlers"]
