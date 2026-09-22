"""Scheduled internet monitoring pass for every institution with monitoring enabled.

Schedule this with cron, an ECS scheduled task, or EventBridge; it exits when
the pass completes. ``--every SECONDS`` keeps it running instead, one pass per
interval (the compose ``monitor`` service uses this). Alerts go to each
profile's alert recipients as in-app notifications (and onward through the
control-plane outbox).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings


async def _run(runtime, institution_id: str | None) -> list[dict[str, object]]:
    platform = runtime.platform
    if platform is None or platform.monitor is None:
        raise SystemExit("internet monitoring is not configured (GURU_INTELLIGENCE_SEARCH_PROVIDER)")
    if institution_id:
        return [await platform.monitor.run_for(institution_id)]
    return await platform.monitor.run_all()


async def _loop(runtime, institution_id: str | None, every: float) -> None:
    while True:
        try:
            print(json.dumps(await _run(runtime, institution_id), indent=2, default=str), flush=True)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - one failed pass must not stop the schedule
            print(json.dumps({"error": str(exc)[:300]}), flush=True)
        await asyncio.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--institution", help="run only this institution (default: every institution with monitoring enabled)")
    parser.add_argument("--every", type=float, default=0.0, help="repeat the pass every SECONDS instead of exiting (minimum 60)")
    args = parser.parse_args()
    if args.every and args.every < 60:
        parser.error("--every must be at least 60 seconds")
    runtime = build_runtime(AppSettings.from_env())
    try:
        if args.every:
            asyncio.run(_loop(runtime, args.institution, args.every))
        else:
            print(json.dumps(asyncio.run(_run(runtime, args.institution)), indent=2, default=str))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
