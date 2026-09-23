"""The run gate: the map's ground truth decides whether new grades may be published.

Two checks guard what readers see:

* canaries: a known look-alike (a gold canary) graded B or better is a
  leak. Canaries are ground truth, so the leak is corrected where it
  stands: the asset gets look-alike evidence (graded D from then on), and a
  person is asked to find which evidence misled the map.
* regressions: when the scoring rule changes, every grade is recomputed as
  a proposal first. The proposal is published only if it keeps canaries
  out and does not lose seed or holdout items the current grades reach, or
  precision; otherwise it waits in the review queue for a manager to
  publish or discard it. Every scheduled pass is held the same way (see
  the engine): what a held pass changed stays unpublished until approved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .metrics import map_metrics
from .pipeline import SECURITY_STATUSES, regrade
from .store import GRADE_RANK, MapStore, grade_at_least

# How much a proposal may lower seed verification, holdout recall or precision.
REGRESSION_TOLERANCE = 0.02


@dataclass(slots=True)
class GateVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        keys = ("assets", "verified", "seed_verification_rate", "holdout_recall", "precision", "canary_leaks")
        return {"passed": self.passed, "reasons": self.reasons, "before": {key: self.before.get(key) for key in keys}, "after": {key: self.after.get(key) for key in keys}}


def compare(before: dict[str, Any], after: dict[str, Any], *, tolerance: float = REGRESSION_TOLERANCE) -> GateVerdict:
    reasons: list[str] = []
    new_leaks = {item["asset_key"] for item in after["canary_leaks"]} - {item["asset_key"] for item in before["canary_leaks"]}
    if new_leaks:
        reasons.append(f"{len(new_leaks)} known look-alike(s) would be graded B or better: {', '.join(sorted(new_leaks))[:200]}")
    for key, label in (("seed_verification_rate", "seed verification"), ("holdout_recall", "holdout recall"), ("precision", "precision")):
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


def snapshot(store: MapStore, institution_id: str) -> dict[str, dict[str, Any]]:
    """What readers see now: every asset's published grade, reasons and rule, before a pass changes them."""

    return {asset["asset_id"]: asset for asset in store.iter_assets(institution_id)}


def hold_changes(store: MapStore, institution_id: str, before: Mapping[str, Mapping[str, Any]], *, run_id: str, canaries: set[str] = frozenset()) -> int:
    """Put back every grade a held pass changed and park the pass's grades as its proposal.

    Two kinds of change are never held back: a downgrade that says a site
    is out of the institution's hands (compromised, hijacked, parked,
    redirected, dead) and the canary corrections themselves.
    """

    held: list[dict[str, Any]] = []
    for asset in store.iter_assets(institution_id):
        previous = before.get(asset["asset_id"])
        was = previous["grade"] if previous is not None else "unrated"
        if asset["grade"] == was or asset["asset_key"] in canaries:
            continue
        if GRADE_RANK.get(asset["grade"], 1) < GRADE_RANK.get(was, 1) and asset["status"] in SECURITY_STATUSES:
            continue
        held.append({
            "asset_id": asset["asset_id"], "before_grade": was, "before_reasons": (previous or {}).get("grade_reasons") or [], "before_scorer": (previous or {}).get("scorer_version"),
            "grade": asset["grade"], "reasons": asset.get("grade_reasons") or [], "scorer": asset.get("scorer_version"),
        })
    return store.hold_changes(institution_id, held, run_id=run_id)


def gate_pass(store: MapStore, institution_id: str, *, before: Mapping[str, Any], published: Mapping[str, Mapping[str, Any]], run_id: str, label: str = "scheduled pass") -> dict[str, Any]:
    """The run gate for one pass: correct canary leaks, compare with the metrics before the pass, and hold it if it regressed.

    Returns the canary incidents, the reasons (empty when it passed), how
    many grade changes were held, and the review item to queue when held.
    """

    leaks = guard_canaries(store, institution_id, run_id=run_id)
    reasons = list(compare(dict(before), map_metrics(store, institution_id)).reasons)
    if leaks:
        reasons.insert(0, f"{len(leaks)} known look-alike(s) reached B or better and were set back: {', '.join(sorted(str(item['target']) for item in leaks))[:200]}")
    held = hold_changes(store, institution_id, published, run_id=run_id, canaries={str(item["target"]) for item in leaks}) if reasons else 0
    review = {
        "kind": "run_gate", "severity": "high", "title": f"A {label} is held: its results wait for approval", "fingerprint": f"run_gate:{run_id}",
        "detail": ("; ".join(reasons) + f". {held} grade change(s) are waiting; publish them or discard them.")[:1500],
    } if reasons else None
    return {"leaks": leaks, "reasons": reasons, "held": held, "review": review}


def rescore(store: MapStore, institution_id: str, *, run_id: str | None = None) -> GateVerdict:
    """Recompute every grade as a proposal (tagged with ``run_id``) and publish it only if the gate passes.

    A held proposal waits for a manager in the review queue; the item names
    the run, so publishing or discarding it acts on this proposal alone.
    """

    before = map_metrics(store, institution_id)
    regrade(store, institution_id, proposed=True, run_id=run_id)
    after = map_metrics(store, institution_id, proposed=True)
    verdict = compare(before, after)
    if verdict.passed:
        store.apply_proposed(institution_id, run_id=run_id)
        guard_canaries(store, institution_id, run_id=run_id)
    else:
        _, outcome = store.add_review_item(
            institution_id, kind="run_gate", severity="high", title="A re-scoring is waiting for approval", detail="; ".join(verdict.reasons)[:1500], run_id=run_id,
            fingerprint=f"run_gate:{run_id}" if run_id else None,
        )
        if outcome == "full":
            verdict.reasons.append("the review queue is full; clear it so this can be decided")
    return verdict


__all__ = ["GateVerdict", "REGRESSION_TOLERANCE", "SECURITY_STATUSES", "compare", "gate_pass", "guard_canaries", "hold_changes", "rescore", "snapshot"]
