"""Guarded entry points to the internet map for routes, agents and scripts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.principals import Capability, InstitutionScope, Principal
from .metrics import map_metrics, record_baseline
from .seed import Lookalike, SeedRow, SeedSummary, import_seed, parse_lookalikes, parse_sweep
from .store import MapStore

MAX_SEED_BYTES = 2_000_000


@dataclass(slots=True)
class MapService:
    store: MapStore

    @staticmethod
    def guard(principal: Principal, institution_id: str, capability: Capability) -> None:
        if not principal.active or not principal.can_access(InstitutionScope(institution_id)):
            raise PermissionError("the requested institution is outside the caller's scope")
        if not principal.has_capability(capability):
            raise PermissionError(f"{capability.value} capability is required")

    # --------------------------------------------------------------- reading
    def assets(self, principal: Principal, institution_id: str, **filters: Any) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        assets = self.store.list_assets(institution_id, **filters)
        if not principal.has_capability(Capability.INTELLIGENCE_MANAGE):
            # Readers see what the map stands behind; unverified and refuted
            # rows are working material for the people who manage it.
            assets = [asset for asset in assets if asset["grade"] in {"O", "A", "A-arch", "B"} or asset["relation"] == "community" and asset["grade"] == "C"]
        return assets

    def entities(self, principal: Principal, institution_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        return self.store.list_entities(institution_id, kind=kind)

    def evidence(self, principal: Principal, institution_id: str, asset_id: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        asset = self.store.get_asset(institution_id, asset_id)
        if asset is None:
            raise KeyError("asset not found")
        return {"asset": asset, "evidence": self.store.list_evidence(institution_id, asset_id=asset_id)}

    def metrics(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        return {**map_metrics(self.store, institution_id), "runs": self.store.list_map_runs(institution_id, limit=10)}

    # --------------------------------------------------------------- seeding
    def seed(
        self, principal: Principal, institution_id: str, *, sweep_text: str, lookalikes_text: str = "", groups: Sequence[str] | None = None, all_groups: bool = False,
        approved_by: str | None = None, holdout_percent: int = 20, source: str = "sweep",
    ) -> dict[str, Any]:
        """Import a sweep. Mapping beyond the caller's own groups needs a named approval.

        A pilot institution maps its own part of the tree; importing every
        group (the Math and its other institutions) is recorded with who
        approved it, because those institutions have not asked to be mapped.
        """

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if len(sweep_text.encode("utf-8")) + len(lookalikes_text.encode("utf-8")) > MAX_SEED_BYTES:
            raise ValueError("the seed files are too large")
        if all_groups and not (approved_by and approved_by.strip()):
            raise ValueError("importing every group needs approved_by: who approved mapping the other institutions")
        if not all_groups and not groups:
            raise ValueError("name the groups to import, or set all_groups with approved_by")
        rows: list[SeedRow] = parse_sweep(sweep_text)
        lookalikes: list[Lookalike] = parse_lookalikes(lookalikes_text) if lookalikes_text.strip() else []
        summary: SeedSummary = import_seed(self.store, institution_id, rows, lookalikes=lookalikes, groups=None if all_groups else groups, holdout_percent=holdout_percent, source=source)
        baseline = record_baseline(self.store, institution_id)
        return {"summary": summary.as_dict(), "baseline": baseline, "approved_by": approved_by if all_groups else None}


__all__ = ["MAX_SEED_BYTES", "MapService"]
