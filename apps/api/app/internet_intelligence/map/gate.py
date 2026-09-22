"""The run gate: the map's ground truth decides whether new grades may be published.

Two checks guard what readers see:

* canaries: a known look-alike (a gold canary) graded B or better is a
  leak. Canaries are ground truth, so the leak is corrected where it
  stands: the asset gets look-alike evidence (graded D from then on), and a
  person is asked to find which evidence misled the map.
* regressions: when the scoring rule changes, every grade is recomputed as
  a proposal first. The proposal is published only if it keeps canaries
  out and does not lose seed or holdout items the current grades reach;
  otherwise it waits in the review queue for a manager to publish or
  discard it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metrics import map_metrics
from .pipeline import regrade
from .store import MapStore, grade_at_least

# How much a proposal may lower seed verification or holdout recall.
REGRESSION_TOLERANCE = 0.02


@dataclass(slots=True)
class GateVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        keys = ("assets", "verified", "seed_verification_rate", "holdout_recall", "canary_leaks")
        return {"passed": self.passed, "reasons": self.reasons, "before": {key: self.before.get(key) for key in keys}, "after": {key: self.after.get(key) for key in keys}}


def compare(before: dict[str, Any], after: dict[str, Any], *, tolerance: float = REGRESSION_TOLERANCE) -> GateVerdict:
    reasons: list[str] = []
    new_leaks = {item["asset_key"] for item in after["canary_leaks"]} - {item["asset_key"] for item in before["canary_leaks"]}
    if new_leaks:
        reasons.append(f"{len(new_leaks)} known look-alike(s) would be graded B or better: {', '.join(sorted(new_leaks))[:200]}")
    for key, label in (("seed_verification_rate", "seed verification"), ("holdout_recall", "holdout recall")):
        was, now = before.get(key), after.get(key)
        if was is not None and now is not None and now < was - tolerance:
            reasons.append(f"{label} would fall from {was:.2%} to {now:.2%}")
    return GateVerdict(not reasons, reasons, before, after)


def guard_canaries(store: MapStore, institution_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
    """Correct and report every canary graded B or better; returns the incidents."""

    incidents: list[dict[str, Any]] = []
    for item in store.list_gold(institution_id, split="canary"):
        asset = store.find_asset(institution_id, item["asset_key"])
        if asset is None or not grade_at_least(asset["grade"], "B"):
            continue
        store.add_evidence(
            institution_id, asset_id=asset["asset_id"], kind="lookalike", polarity="refutes", detail=f"ground truth: a known look-alike ({item['entity_name'][:80]})",
            channel="gold", observed_via="import", run_id=run_id,
        )
        regrade(store, institution_id, [asset["asset_id"]])
        store.add_review_item(
            institution_id, kind="canary_leak", severity="high", asset_id=asset["asset_id"], title=f"A known look-alike reached grade {asset['grade']}: {asset['handle']}",
            detail="It has been set back to D. Check its evidence for the source that vouched for it; that source may be compromised or wrong.", url=asset["url"], run_id=run_id,
        )
        incidents.append({"kind": "canary_leak", "target": asset["asset_key"], "signals": [f"graded {asset['grade']}"], "severity": "high"})
    return incidents


def rescore(store: MapStore, institution_id: str, *, run_id: str | None = None) -> GateVerdict:
    """Recompute every grade as a proposal and publish it only if the gate passes."""

    before = map_metrics(store, institution_id)
    regrade(store, institution_id, proposed=True)
    after = map_metrics(store, institution_id, proposed=True)
    verdict = compare(before, after)
    if verdict.passed:
        store.apply_proposed(institution_id)
    else:
        store.add_review_item(
            institution_id, kind="run_gate", severity="high", title="A re-scoring is waiting for approval", detail="; ".join(verdict.reasons)[:1500], run_id=run_id,
            fingerprint=f"run_gate:{run_id}" if run_id else None,
        )
    return verdict


__all__ = ["GateVerdict", "REGRESSION_TOLERANCE", "compare", "guard_canaries", "rescore"]
