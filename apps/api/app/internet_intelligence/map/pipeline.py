"""Recompute grades from evidence, and bring the institution profile into the map."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from ..profile import InstitutionProfile
from .assets import asset_ref
from .grading import SCORER_VERSION, apply_disputes, grade
from .store import MapStore


def regrade(store: MapStore, institution_id: str, asset_ids: Iterable[str] | None = None, *, now: datetime | None = None, proposed: bool = False) -> list[dict[str, Any]]:
    """Recompute grades (all assets, or the given ones); returns the changes.

    ``proposed`` parks the new grades in ``proposed_grade`` instead of
    publishing them, for a run whose results a gate must approve first.
    """

    current = now or datetime.now(timezone.utc)
    assets = {asset["asset_id"]: asset for asset in store.list_assets(institution_id, limit=5000)}
    wanted = list(dict.fromkeys(asset_ids)) if asset_ids is not None else list(assets)
    evidence_by_asset: dict[str, list[dict[str, Any]]] = {}
    for item in store.list_evidence(institution_id, limit=20000):
        evidence_by_asset.setdefault(item["asset_id"], []).append(item)
    results = {asset_id: grade(assets[asset_id], evidence_by_asset.get(asset_id, []), now=current) for asset_id in wanted if asset_id in assets}
    grades = {asset_id: asset["grade"] for asset_id, asset in assets.items()}
    grades.update({asset_id: result.grade for asset_id, result in results.items()})
    disputed = apply_disputes(assets.values(), grades)
    changes: list[dict[str, Any]] = []
    for asset_id, result in results.items():
        asset = assets[asset_id]
        status = "disputed" if asset_id in disputed else result.status
        if status and status != asset["status"]:
            store.set_status(institution_id, asset_id, status=status, verified_via=result.verified_via, verified_at=result.verified_at)
        elif result.verified_at and result.verified_at != asset.get("last_verified_at"):
            store.set_status(institution_id, asset_id, status=asset["status"], verified_via=result.verified_via, verified_at=result.verified_at)
        if result.grade != asset["grade"] or result.reasons != asset.get("grade_reasons"):
            store.set_grade(institution_id, asset_id, grade=result.grade, reasons=result.reasons, scorer_version=SCORER_VERSION, proposed=proposed and result.grade != asset["grade"])
        if result.grade != asset["grade"]:
            changes.append({"asset_id": asset_id, "asset_key": asset["asset_key"], "from": asset["grade"], "to": result.grade, "reasons": result.reasons})
    return changes


def sync_profile(store: MapStore, profile: InstitutionProfile, *, run_id: str | None = None) -> dict[str, Any]:
    """Make the profile's institution an entity and its configured domains graded assets.

    A domain the institution's own managers configured is operator input:
    it anchors the map (grade A while live and healthy). The evidence is
    added once per domain, not on every sync.
    """

    entity_id = store.upsert_entity(profile.institution_id, name=profile.name, kind="institution", names=list(profile.aliases), locations=[profile.location] if profile.location else [])
    created: list[str] = []
    for domain in profile.official_domains:
        ref = asset_ref(f"https://{domain}/")
        asset_id, is_new = store.upsert_asset(profile.institution_id, ref, entity_id=entity_id, relation="official", note="configured in the intelligence profile")
        existing = [item for item in store.list_evidence(profile.institution_id, asset_id=asset_id) if item["kind"] == "configured_domain"]
        if not existing:
            store.add_evidence(profile.institution_id, asset_id=asset_id, kind="configured_domain", detail=f"profile of {profile.name}", channel="profile", observed_via="reviewer", run_id=run_id)
        if is_new:
            created.append(asset_id)
    # Profile social handles are bare ("@bgscet") and cannot be placed on a
    # platform without guessing; they stay in the profile for entity resolution.
    return {"entity_id": entity_id, "domains_added": created}


def anchor_grade(store: MapStore, institution_id: str, asset_id: str | None) -> str:
    if not asset_id:
        return "C"
    asset = store.get_asset(institution_id, asset_id)
    if asset is None:
        return "C"
    if asset["status"] in {"parked", "hijacked", "compromised", "dead"}:
        return "D"
    return str(asset["grade"])


def unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


__all__ = ["anchor_grade", "regrade", "sync_profile", "unique"]
