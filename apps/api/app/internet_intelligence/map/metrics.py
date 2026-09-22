"""How good the map is, measured against ground truth it was never shown.

* Holdout recall: of the hidden sweep items, how many the map found again at
  the grade the sweep expected. This is the honest "reaches more" number.
* Seed verification: of the seeded claims, how many the map has itself
  re-verified to the expected grade (a seed only counts once re-observed).
* Canary leaks: look-alikes graded as an official channel at B or better.
  The target is zero on every run.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .store import GRADE_RANK, MapStore, grade_at_least


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def map_metrics(store: MapStore, institution_id: str) -> dict[str, Any]:
    assets = store.list_assets(institution_id, limit=5000)
    by_key = {asset["asset_key"]: asset for asset in assets}
    gold = store.list_gold(institution_id)
    holdout = [item for item in gold if item["split"] == "holdout"]
    seeds = [item for item in gold if item["split"] == "seed"]
    canaries = [item for item in gold if item["split"] == "canary"]

    def reached(item: dict[str, Any]) -> bool:
        asset = by_key.get(item["asset_key"])
        return asset is not None and grade_at_least(asset["grade"], item["expected_min_grade"])

    holdout_found = [item for item in holdout if item["asset_key"] in by_key]
    holdout_reached = [item for item in holdout if reached(item)]
    seeds_verified = [item for item in seeds if reached(item)]
    leaks = [
        {"asset_key": item["asset_key"], "entity_name": item["entity_name"], "grade": by_key[item["asset_key"]]["grade"]}
        for item in canaries
        if item["asset_key"] in by_key and by_key[item["asset_key"]]["relation"] == "official" and grade_at_least(by_key[item["asset_key"]]["grade"], "B")
    ]
    per_platform: dict[str, dict[str, int]] = {}
    for item in holdout:
        bucket = per_platform.setdefault(item["platform"], {"holdout": 0, "reached": 0})
        bucket["holdout"] += 1
        bucket["reached"] += int(reached(item))
    grades = Counter(asset["grade"] for asset in assets)
    return {
        "assets": len(assets),
        "by_grade": {grade: grades.get(grade, 0) for grade in sorted(GRADE_RANK, key=lambda grade: -GRADE_RANK[grade])},
        "by_platform": dict(Counter(asset["platform"] for asset in assets).most_common()),
        "by_relation": dict(Counter(asset["relation"] for asset in assets).most_common()),
        "by_status": dict(Counter(asset["status"] for asset in assets).most_common()),
        "verified": sum(1 for asset in assets if grade_at_least(asset["grade"], "B")),
        "holdout_total": len(holdout),
        "holdout_found": len(holdout_found),
        "holdout_recall": _ratio(len(holdout_reached), len(holdout)),
        "holdout_recall_by_platform": {platform: {**bucket, "recall": _ratio(bucket["reached"], bucket["holdout"])} for platform, bucket in sorted(per_platform.items())},
        "seed_total": len(seeds),
        "seed_verified": len(seeds_verified),
        "seed_verification_rate": _ratio(len(seeds_verified), len(seeds)),
        "canary_total": len(canaries),
        "canary_leaks": leaks,
    }


def record_baseline(store: MapStore, institution_id: str) -> dict[str, Any]:
    """Store the current metrics as a baseline run so later runs can be compared with it."""

    run_id = store.start_map_run(institution_id, kind="baseline")
    metrics = map_metrics(store, institution_id)
    store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="baseline", metrics=metrics)
    return {"run_id": run_id, **metrics}


__all__ = ["map_metrics", "record_baseline"]
