"""How good the map is, measured against ground truth it was never shown.

* Holdout recall: of the hidden sweep items, how many the map found again at
  the grade the sweep expected. This is the honest "reaches more" number.
* Seed verification: of the seeded claims, how many the map has itself
  re-verified to the expected grade. A seed counts only once something other
  than the import supports it: an imported claim alone grades C, the expected
  grade of every non-official row, so otherwise the first regrade "verified"
  half the sweep with no evidence at all.
* Canary leaks: look-alikes graded as an official channel at B or better.
  The target is zero on every run.
* Precision: of what readers are shown (B or better), the share not known to
  be wrong. Recall says the map reaches more; precision says it did not buy
  that by letting wrong accounts through.
* Freshness: how recently what readers are shown was checked, and how far
  the engine is behind its schedule.
* The run series: per tick, what it did, what it cost and whether recall
  moved, so "does each run re-find more of the hidden list without letting a
  known look-alike through?" is read off one table.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import GRADE_RANK, MapStore, grade_at_least

FRESHNESS_WINDOWS = (7, 30, 90)
# Evidence that an asset is not the institution's, and what overturns it (as the grader reads them).
_REJECTIONS = frozenset({"reviewer_reject", "lookalike", "impersonation"})
_CONFIRMATIONS = frozenset({"owner_claim", "reviewer_confirm"})


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _rejected(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Refuted by a reviewer, as a look-alike or as an impersonator, with no owner or reviewer confirmation since (rows oldest first)."""

    rejected = False
    for row in rows:
        if row["kind"] in _REJECTIONS and row["polarity"] == "refutes":
            rejected = True
        elif row["kind"] in _CONFIRMATIONS and row["polarity"] == "supports":
            rejected = False
    return rejected


def _age_days(stamp: Any, now: datetime) -> float | None:
    try:
        moment = datetime.fromisoformat(str(stamp)) if stamp else None
    except ValueError:
        return None
    if moment is None:
        return None
    return max(0.0, (now - (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))).total_seconds() / 86400)


def _freshness(store: MapStore, institution_id: str, verified: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """How recently the B-or-better assets were last verified, and how far behind the engine is.

    ``within_<n>_days`` is the share of B-or-better assets whose last
    verification (a live fetch, the owner or a reviewer; a search snippet
    never counts) is at most n days old; one never verified that way counts
    as outside every window. ``median_age_days`` is over those verified at
    least once. ``sources_overdue`` counts active sources more than a day past
    due: work the budget or the tick size is not keeping up with.
    """

    ages = sorted(age for age in (_age_days(asset.get("last_verified_at"), now) for asset in verified) if age is not None)
    cutoff = (now - timedelta(days=1)).isoformat()
    # Sources come oldest-due first, so counting stops at the first one not overdue.
    overdue = 0
    for source in store.list_sources(institution_id, status="active", limit=10000):
        if str(source["due_at"]) >= cutoff:
            break
        overdue += 1
    return {
        "verified": len(verified), "never_verified": len(verified) - len(ages),
        **{f"within_{days}_days": _ratio(sum(1 for age in ages if age <= days), len(verified)) for days in FRESHNESS_WINDOWS},
        "median_age_days": round(statistics.median(ages), 1) if ages else None, "sources_overdue": overdue,
    }


def map_metrics(store: MapStore, institution_id: str, *, proposed: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """How the map measures up against its ground truth; ``proposed`` measures the grades a run is waiting to publish.

    ``precision`` is, of the assets graded B or better, the share that is not
    known to be wrong, where wrong means a canary (a gold look-alike), an asset
    of a look-alike entity, or an asset a reviewer rejected or that carries
    look-alike or impersonation evidence with no owner or reviewer
    confirmation since. It is None when nothing is at B or better. The grader
    sends refuted assets to D, so precision falls when a look-alike is let
    through; a run gate holds a run whose precision drops.
    """

    current = now or datetime.now(timezone.utc)
    assets = list(store.iter_assets(institution_id))
    if proposed:
        assets = [{**asset, "grade": asset.get("proposed_grade") or asset["grade"]} for asset in assets]
    by_key = {asset["asset_key"]: asset for asset in assets}
    gold = store.list_gold(institution_id)
    holdout = [item for item in gold if item["split"] == "holdout"]
    seeds = [item for item in gold if item["split"] == "seed"]
    canaries = [item for item in gold if item["split"] == "canary"]
    verified = [asset for asset in assets if grade_at_least(asset["grade"], "B")]
    seeded = [by_key[item["asset_key"]]["asset_id"] for item in seeds if item["asset_key"] in by_key]
    evidence = store.evidence_for(institution_id, [asset["asset_id"] for asset in verified] + seeded)

    def reached(item: dict[str, Any]) -> bool:
        asset = by_key.get(item["asset_key"])
        return asset is not None and grade_at_least(asset["grade"], item["expected_min_grade"])

    def observed(item: dict[str, Any]) -> bool:
        """Something other than the import itself supports the seed."""

        return any(row["polarity"] == "supports" and row["observed_via"] != "import" for row in evidence.get(by_key[item["asset_key"]]["asset_id"], ()))

    holdout_found = [item for item in holdout if item["asset_key"] in by_key]
    holdout_reached = [item for item in holdout if reached(item)]
    seeds_verified = [item for item in seeds if reached(item) and observed(item)]
    leaks = [
        {"asset_key": item["asset_key"], "entity_name": item["entity_name"], "grade": by_key[item["asset_key"]]["grade"]}
        for item in canaries
        # Whatever its relation, a canary at B or better is shown to readers: a leak.
        if item["asset_key"] in by_key and grade_at_least(by_key[item["asset_key"]]["grade"], "B")
    ]
    canary_keys = {item["asset_key"] for item in canaries}
    lookalike_entities = {entity["entity_id"] for entity in store.list_entities(institution_id, kind="lookalike", limit=5000)}
    wrong = [asset for asset in verified if asset["asset_key"] in canary_keys or asset["entity_id"] in lookalike_entities or _rejected(evidence.get(asset["asset_id"], ()))]
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
        "verified": len(verified),
        "precision": _ratio(len(verified) - len(wrong), len(verified)),
        "freshness": _freshness(store, institution_id, verified, current),
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


def run_series(store: MapStore, institution_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """The last ``limit`` ticks, newest first: what each did, what it cost, and whether the map reached more.

    The changes compare with the tick before (skipping one that stored no
    metrics, such as a failed tick). ``canary_leaks_caught`` is how many known
    look-alikes the tick found at B or better and set back: its stored
    metrics are taken after that correction, so they never show the leak.
    ``spend`` is the budget units the tick reserved (requests, or a
    provider's quota units; not money), and ``cost_per_verified`` that spend
    over the verified items gained since the tick before (None when none).
    """

    series: list[dict[str, Any]] = []
    previous: dict[str, Any] = {}
    # One tick more than shown, so the oldest shown still has a tick to compare with.
    for run in reversed(store.list_map_runs(institution_id, kind="tick", limit=limit + 1)):
        metrics, counts = run["metrics"], run["counts"]
        recall, verified = metrics.get("holdout_recall"), metrics.get("verified")
        spend = round(sum(float(units) for units in run["spend"].values()), 2)
        gained = verified - previous["verified"] if verified is not None and "verified" in previous else None
        series.append({
            "run_id": run["run_id"], "started_at": run["started_at"], "status": run["status"], "gate": run["gate"], "stop_reason": run["stop_reason"],
            **{key: counts.get(key) for key in ("sources", "new_assets", "raised", "leads", "canary_leaks_caught")},
            "verified": verified, "verified_change": gained, "holdout_recall": recall,
            "holdout_recall_change": round(recall - previous["holdout_recall"], 4) if recall is not None and "holdout_recall" in previous else None,
            "precision": metrics.get("precision"), "spend": spend, "spend_by_budget": run["spend"], "cost_per_verified": round(spend / gained, 2) if gained and gained > 0 else None,
        })
        previous.update({key: value for key, value in (("verified", verified), ("holdout_recall", recall)) if value is not None})
    return series[::-1][:limit]


def record_baseline(store: MapStore, institution_id: str, *, import_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store the current metrics as a baseline run so later runs can be compared with it.

    ``import_record`` (who imported which groups, and who approved it) is
    kept with the run, in the tenant's own data.
    """

    run_id = store.start_map_run(institution_id, kind="baseline")
    metrics = map_metrics(store, institution_id)
    store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="baseline", metrics=metrics, counts={"import": import_record} if import_record else None)
    return {"run_id": run_id, **metrics}


__all__ = ["FRESHNESS_WINDOWS", "map_metrics", "record_baseline", "run_series"]
