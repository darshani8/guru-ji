"""Job queue with inline, threaded, and SQS-backed execution.

Jobs are always recorded in the institution store's ``background_jobs`` table
so their status can be queried regardless of the transport that wakes a
worker. Handlers are async callables registered by job type.

Liveness: while a handler runs, a heartbeat thread refreshes the job's
``heartbeat_at``; the stale sweep (at boot and periodically from the worker
loops) hands back only jobs whose heartbeat stopped, so a job that legitimately
runs for a long time is never executed twice, while one whose worker died is
resumed within the stale window rather than after a restart that happens to
come later.
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
FailureHook = Callable[[Mapping[str, Any], str], None]
DEFAULT_STALE_SECONDS = 180.0
DEFAULT_HEARTBEAT_SECONDS = 30.0


class _Heartbeat:
    """Refreshes a running job's heartbeat from its own thread until stopped."""

    def __init__(self, store: InstitutionDataStore, job_id: str, interval_seconds: float) -> None:
        self._store = store
        self._job_id = job_id
        self._interval = max(0.01, float(interval_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"saffron-heartbeat-{job_id[-8:]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._store.heartbeat_background_job(self._job_id)
            except Exception:  # noqa: BLE001 - a missed beat is tolerated; the next one retries
                continue


class JobQueue:
    """Base queue: records jobs and runs handlers; subclasses decide when."""

    backend_name = "inline"

    def __init__(self, store: InstitutionDataStore, *, worker_store: InstitutionDataStore | None = None, stale_seconds: float = DEFAULT_STALE_SECONDS, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS) -> None:
        # ``store`` serves the request path (enqueue, status); ``worker_store``
        # is what the worker uses to claim, heartbeat and finish jobs, so a
        # worker's long transaction never holds the request path's lock.
        self.store = store
        self.worker_store = worker_store or store
        self.stale_seconds = float(stale_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._handlers: dict[str, JobHandler] = {}
        self._failure_hooks: dict[str, FailureHook] = {}
        self._lock = threading.RLock()
        self._current_job_id: str | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    def register_failure_hook(self, job_type: str, hook: FailureHook) -> None:
        """Called with (job, error) when a job of this type is abandoned by the queue itself."""

        self._failure_hooks[job_type] = hook

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

    def recover_stale(self, *, older_than_seconds: float | None = None, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Return jobs whose worker stopped reporting to the queue and dispatch them again.

        A job still ``running`` whose heartbeat is older than the stale window
        has no live worker (a crash or restart left it behind), so it goes back
        to ``queued`` and is re-dispatched the same way a fresh enqueue is. A
        job that cannot be re-dispatched, or has used up ``max_attempts``, is
        marked failed rather than left queued forever, and the failure hook for
        its type (if any) is told.
        """

        window = self.stale_seconds if older_than_seconds is None else float(older_than_seconds)
        exhausted: list[dict[str, Any]] = []
        requeued = self.worker_store.requeue_stale_background_jobs(older_than_seconds=window, max_attempts=max_attempts, exhausted=exhausted)
        for job in exhausted:
            # Failed by the store for using up its attempts: whoever waits on it is told too.
            self._notify_failure(job, f"worker did not finish the job after {int(job.get('attempts') or max_attempts)} attempts")
        for job in requeued:
            job_id = str(job["job_id"])
            try:
                self._after_enqueue(job_id)
            except Exception as exc:  # noqa: BLE001 - the job record captures the failure
                error = f"could not re-dispatch after restart: {exc}"[:500]
                self.worker_store.finish_background_job(job_id, status="failed", error=error)
                self._notify_failure(job, error)
        return requeued

    def _notify_failure(self, job: Mapping[str, Any], error: str) -> None:
        hook = self._failure_hooks.get(str(job.get("job_type")))
        if hook is None:
            return
        try:
            hook(job, error)
        except Exception:  # noqa: BLE001 - a hook must never break the queue
            return

    async def run_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        handler = self._handlers.get(str(job["job_type"]))
        if handler is None:
            self.worker_store.finish_background_job(job_id, status="failed", error="no handler registered")
            return {"job_id": job_id, "status": "failed"}
        # Handlers learn which attempt this is: a re-dispatched job (attempt > 1)
        # is known to have lost its previous worker.
        attempt = int(job.get("attempts") or 1)
        payload = {**dict(job.get("payload") or {}), "_attempt": attempt}
        pulse = _Heartbeat(self.worker_store, job_id, self.heartbeat_seconds)
        self._current_job_id = job_id
        pulse.start()
        try:
            result = await handler(payload)
        except Exception as exc:  # noqa: BLE001 - the job record captures the failure
            pulse.stop()
            self._current_job_id = None
            self.worker_store.finish_background_job(job_id, status="failed", error=str(exc)[:500], attempt=attempt)
            return {"job_id": job_id, "status": "failed", "error": str(exc)[:500]}
        pulse.stop()
        self._current_job_id = None
        self.worker_store.finish_background_job(job_id, status="succeeded", result=result, attempt=attempt)
        return {"job_id": job_id, "status": "succeeded"}

    async def run_pending(self, limit: int = 10) -> int:
        completed = 0
        for job in self.worker_store.claim_background_jobs(limit):
            await self.run_job(job)
            completed += 1
        return completed

    def run_pending_blocking(self, limit: int = 10) -> int:
        """Run pending jobs from synchronous code, even when an event loop is active in this thread."""

        result: dict[str, int] = {}

        def _target() -> None:
            result["count"] = asyncio.run(self.run_pending(limit))

        thread = threading.Thread(target=_target, name="saffron-jobs-inline", daemon=True)
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

    def __init__(self, store: InstitutionDataStore, poll_seconds: float = 1.0, *, worker_store: InstitutionDataStore | None = None, stale_seconds: float = DEFAULT_STALE_SECONDS, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS, sweep_seconds: float = 60.0) -> None:
        super().__init__(store, worker_store=worker_store, stale_seconds=stale_seconds, heartbeat_seconds=heartbeat_seconds)
        self.poll_seconds = poll_seconds
        self.sweep_seconds = float(sweep_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the polling thread; safe to call repeatedly (idempotent)."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="saffron-jobs", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop polling and wait for the job in flight; one that outlives the wait goes back to the queue.

        The next process then resumes it immediately instead of after the
        stale window, and the outcome of the abandoned run cannot overwrite the
        resumed one (a finished job keeps its first outcome).
        """

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive() and self._current_job_id:
                try:
                    self.worker_store.requeue_background_job(self._current_job_id)
                except Exception:  # noqa: BLE001 - the stale sweep covers it later
                    pass

    def _after_enqueue(self, job_id: str) -> None:
        self.start()
        self._wake.set()

    def _loop(self) -> None:
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            try:
                asyncio.run(self.run_pending(10))
                if time.monotonic() - last_sweep >= self.sweep_seconds:
                    last_sweep = time.monotonic()
                    self.recover_stale()
            except Exception:  # noqa: BLE001 - keep the worker alive
                time.sleep(self.poll_seconds)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()


class SqsJobQueue(JobQueue):
    """Records the job and publishes its id to SQS; ``scripts/run_worker.py`` consumes it."""

    backend_name = "sqs"

    def __init__(self, store: InstitutionDataStore, queue_url: str, *, region: str | None = None, client: Any | None = None, stale_seconds: float = DEFAULT_STALE_SECONDS, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS) -> None:
        super().__init__(store, stale_seconds=stale_seconds, heartbeat_seconds=heartbeat_seconds)
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
            job = self.worker_store.claim_background_job(job_id) if job_id else None
            if job is not None:
                await self.run_job(job)
                handled += 1
            self._client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        return handled


__all__ = ["DEFAULT_HEARTBEAT_SECONDS", "DEFAULT_STALE_SECONDS", "FailureHook", "InlineJobQueue", "JobHandler", "JobQueue", "SqsJobQueue", "ThreadJobQueue"]
