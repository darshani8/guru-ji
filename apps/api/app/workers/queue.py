"""Job queue with inline, threaded, and SQS-backed execution.

Jobs are always recorded in the institution store's ``background_jobs`` table
so their status can be queried regardless of the transport that wakes a
worker. Handlers are async callables registered by job type.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from ..institution_data.store import InstitutionDataStore

JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class JobQueue:
    """Base queue: records jobs and runs handlers; subclasses decide when."""

    backend_name = "inline"

    def __init__(self, store: InstitutionDataStore) -> None:
        self.store = store
        self._handlers: dict[str, JobHandler] = {}
        self._lock = threading.RLock()

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    def handlers(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def enqueue(self, institution_id: str, job_type: str, payload: Mapping[str, Any]) -> str:
        if job_type not in self._handlers:
            raise ValueError(f"no handler registered for job type {job_type}")
        job_id = f"bg-{uuid4().hex}"
        self.store.enqueue_background_job(institution_id, job_id=job_id, job_type=job_type, payload=dict(payload))
        self._after_enqueue(job_id)
        return job_id

    def _after_enqueue(self, job_id: str) -> None:
        return None

    def status(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get_background_job(job_id)

    def recover_stale(self, *, older_than_seconds: int, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Return jobs interrupted by a restart to the queue and dispatch them again.

        A job still ``running`` after ``older_than_seconds`` has no live worker
        (a process restart or crash left it behind), so it goes back to
        ``queued`` and is re-dispatched the same way a fresh enqueue is. A job
        that cannot be re-dispatched is marked failed rather than left queued
        forever, so the failure is visible in its record.
        """

        requeued = self.store.requeue_stale_background_jobs(older_than_seconds=older_than_seconds, max_attempts=max_attempts)
        for job in requeued:
            job_id = str(job["job_id"])
            try:
                self._after_enqueue(job_id)
            except Exception as exc:  # noqa: BLE001 - the job record captures the failure
                self.store.finish_background_job(job_id, status="failed", error=f"could not re-dispatch after restart: {exc}"[:500])
        return requeued

    async def run_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(str(job["job_type"]))
        if handler is None:
            self.store.finish_background_job(str(job["job_id"]), status="failed", error="no handler registered")
            return {"job_id": job["job_id"], "status": "failed"}
        try:
            result = await handler(dict(job.get("payload") or {}))
        except Exception as exc:  # noqa: BLE001 - the job record captures the failure
            self.store.finish_background_job(str(job["job_id"]), status="failed", error=str(exc)[:500])
            return {"job_id": job["job_id"], "status": "failed", "error": str(exc)[:500]}
        self.store.finish_background_job(str(job["job_id"]), status="succeeded", result=result)
        return {"job_id": job["job_id"], "status": "succeeded"}

    async def run_pending(self, limit: int = 10) -> int:
        completed = 0
        for job in self.store.claim_background_jobs(limit):
            await self.run_job(job)
            completed += 1
        return completed

    def run_pending_blocking(self, limit: int = 10) -> int:
        """Run pending jobs from synchronous code, even when an event loop is active in this thread."""

        result: dict[str, int] = {}

        def _target() -> None:
            result["count"] = asyncio.run(self.run_pending(limit))

        thread = threading.Thread(target=_target, name="guru-jobs-inline", daemon=True)
        thread.start()
        thread.join()
        return result.get("count", 0)


class InlineJobQueue(JobQueue):
    """Runs each job right after it is enqueued; deterministic for tests and small pilots."""

    backend_name = "inline"

    def _after_enqueue(self, job_id: str) -> None:
        self.run_pending_blocking(limit=25)


class ThreadJobQueue(JobQueue):
    """A background thread polls the job table; suitable for a single-process deployment."""

    backend_name = "thread"

    def __init__(self, store: InstitutionDataStore, poll_seconds: float = 1.0) -> None:
        super().__init__(store)
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the polling thread; safe to call repeatedly (idempotent)."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="guru-jobs", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _after_enqueue(self, job_id: str) -> None:
        self.start()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self.run_pending(10))
            except Exception:  # noqa: BLE001 - keep the worker alive
                time.sleep(self.poll_seconds)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()


class SqsJobQueue(JobQueue):
    """Records the job and publishes its id to SQS; ``scripts/run_worker.py`` consumes it."""

    backend_name = "sqs"

    def __init__(self, store: InstitutionDataStore, queue_url: str, *, region: str | None = None, client: Any | None = None) -> None:
        super().__init__(store)
        if not queue_url.startswith("https://"):
            raise ValueError("SQS queue URL must be an HTTPS URL")
        self.queue_url = queue_url
        if client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("SQS job queue requires the optional boto3 dependency") from exc
            client = boto3.client("sqs", region_name=region) if region else boto3.client("sqs")
        self._client = client

    def _after_enqueue(self, job_id: str) -> None:
        try:
            self._client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({"job_id": job_id}))
        except Exception as exc:  # noqa: BLE001 - any transport/permission error means nobody will run the job
            # The row was already inserted as ``queued``; without a message no
            # worker will ever pick it up, so record the failure instead of
            # leaving a job that looks pending forever.
            self.store.finish_background_job(job_id, status="failed", error=f"could not publish to SQS: {exc}"[:500])
            raise RuntimeError(f"background job {job_id} could not be published to the job queue") from exc

    async def consume_once(self, *, wait_seconds: int = 10, max_messages: int = 5) -> int:
        response = self._client.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait_seconds)
        handled = 0
        for message in response.get("Messages", []):
            try:
                body = json.loads(message.get("Body", "{}"))
                job_id = str(body.get("job_id", ""))
            except ValueError:
                job_id = ""
            # Claim only the job this message names: other queued jobs belong to
            # their own messages (possibly on another worker) and must stay queued.
            job = self.store.claim_background_job(job_id) if job_id else None
            if job is not None:
                await self.run_job(job)
                handled += 1
            self._client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        return handled


__all__ = ["InlineJobQueue", "JobHandler", "JobQueue", "SqsJobQueue", "ThreadJobQueue"]
