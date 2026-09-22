"""Background worker: drains queued jobs (ingestion, long agent commands, monitoring).

For GURU_JOB_QUEUE=sqs it long-polls the queue; otherwise it polls the job table.
Run one or more copies next to the API in deployments that do not use the
in-process thread queue.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings


async def _loop(runtime, once: bool) -> None:
    platform = runtime.platform
    if platform is None:
        raise SystemExit("the data platform is disabled; nothing to process")
    queue = platform.jobs
    consume = getattr(queue, "consume_once", None)
    sweep_seconds = float(os.getenv("GURU_WORKER_SWEEP_SECONDS", "60"))
    last_sweep = 0.0
    while True:
        if time.monotonic() - last_sweep >= sweep_seconds:
            # Hand back jobs whose worker stopped reporting (crash, restart).
            last_sweep = time.monotonic()
            requeued = queue.recover_stale()
            if requeued:
                print(f"worker: requeued {len(requeued)} stale job(s)", flush=True)
        if callable(consume):
            handled = await consume(wait_seconds=10)
        else:
            handled = await queue.run_pending(limit=10)
        print(f"worker: handled {handled} job(s)", flush=True)
        if once:
            return
        if not handled and not callable(consume):
            time.sleep(float(os.getenv("GURU_WORKER_POLL_SECONDS", "2")))


def main() -> None:
    settings = AppSettings.from_env()
    runtime = build_runtime(settings)
    try:
        asyncio.run(_loop(runtime, once="--once" in sys.argv))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
