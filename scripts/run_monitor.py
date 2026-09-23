"""Scheduled internet monitoring pass for every institution with monitoring enabled.

Schedule this with cron, an ECS scheduled task, or EventBridge; it exits when
the pass completes. ``--every SECONDS`` keeps it running instead, one pass per
interval (the compose ``monitor`` service uses this). ``--map`` runs the
internet map engine instead: one tick per institution with monitoring
enabled, each picking up where the last stopped. ``--digest`` sends each
institution's map digest (open incidents, the review queue, what was found
since the last digest) once its last one is 23 hours old, then applies the
intelligence retention period (GURU_INTELLIGENCE_RETENTION_DAYS) to that
institution's data; run it hourly (``--digest --every 3600``) so a failed
digest is retried within the hour and a restart never sends one twice.
Alerts go to each
profile's alert recipients as in-app notifications (and onward through the
control-plane outbox).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings


async def _run(runtime, institution_id: str | None, map_mode: bool = False, digest: bool = False) -> list[dict[str, object]]:
    platform = runtime.platform
    if map_mode or digest:
        if platform is None or platform.intelligence_map is None or platform.intelligence_map.engine is None:
            raise SystemExit("the internet map is not enabled (GURU_INTELLIGENCE_MAP_ENABLED)")
        institutions = [institution_id] if institution_id else platform.intelligence_store.monitored_institutions()
        if digest:
            desk, days = platform.intelligence_map.desk, runtime.settings.intelligence_retention_days
            desk.store.cache_prune()  # the shared public-web cache: drop what expired
            results: list[dict[str, object]] = []
            for item in institutions:
                try:
                    digest = desk.send_digest(item, only_if_due=True)
                    entry: dict[str, object] = {key: value for key, value in digest.items() if key != "incidents"}
                    if digest.get("due", True):
                        entry["pruned"] = _prune(platform, days, item)  # once a day, with the digest
                    results.append(entry)
                except Exception as exc:  # noqa: BLE001 - one institution's failure must not hold back the others' digests
                    results.append({"institution_id": item, "error": str(exc)[:300]})
            return results
        return await platform.intelligence_map.engine.tick_all(institutions)
    if platform is None or platform.monitor is None:
        raise SystemExit("internet monitoring is not configured (GURU_INTELLIGENCE_SEARCH_PROVIDER)")
    if institution_id:
        return [await platform.monitor.run_for(institution_id)]
    return await platform.monitor.run_all()


def _prune(platform, days: int, institution_id: str) -> dict[str, object]:
    """Retention rides on the daily digest; a failure is reported, not raised (the next pass would send the digest again)."""

    try:
        return platform.prune_intelligence(days, institution_id=institution_id)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not stop the digests
        return {"error": str(exc)[:300]}


async def _loop(runtime, institution_id: str | None, every: float, map_mode: bool, digest: bool = False) -> None:
    while True:
        try:
            print(json.dumps(await _run(runtime, institution_id, map_mode, digest), indent=2, default=str), flush=True)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - one failed pass must not stop the schedule
            print(json.dumps({"error": str(exc)[:300]}), flush=True)
        await asyncio.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--institution", help="run only this institution (default: every institution with monitoring enabled)")
    parser.add_argument("--every", type=float, default=0.0, help="repeat the pass every SECONDS instead of exiting (minimum 60)")
    parser.add_argument("--map", action="store_true", help="run internet-map engine ticks instead of the mention monitor")
    parser.add_argument("--digest", action="store_true", help="send each institution's internet-map digest when its last one is 23 hours old (run hourly)")
    args = parser.parse_args()
    if args.every and args.every < 60:
        parser.error("--every must be at least 60 seconds")
    runtime = build_runtime(AppSettings.from_env())
    try:
        if args.every:
            asyncio.run(_loop(runtime, args.institution, args.every, args.map, args.digest))
        else:
            print(json.dumps(asyncio.run(_run(runtime, args.institution, args.map, args.digest)), indent=2, default=str))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
