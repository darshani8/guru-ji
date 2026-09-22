"""Scheduled internet monitoring pass for every institution with monitoring enabled.

Schedule this with cron, an ECS scheduled task, or EventBridge; it exits when
the pass completes. Alerts go to each profile's alert recipients as in-app
notifications (and onward through the control-plane outbox).
"""

from __future__ import annotations

import asyncio
import json

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings


async def _run(runtime) -> list[dict[str, object]]:
    platform = runtime.platform
    if platform is None or platform.monitor is None:
        raise SystemExit("internet monitoring is not configured (GURU_INTELLIGENCE_SEARCH_PROVIDER)")
    return await platform.monitor.run_all()


def main() -> None:
    runtime = build_runtime(AppSettings.from_env())
    try:
        results = asyncio.run(_run(runtime))
    finally:
        runtime.close()
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
