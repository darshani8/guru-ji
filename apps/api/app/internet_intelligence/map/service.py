"""Guarded entry points to the internet map for routes, agents and scripts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.principals import Capability, InstitutionScope, Principal
from ..fetch import PublicPageFetcher
from ..profile import InstitutionProfile
from .engine import MapEngine
from .export import export_tsv
from .harvest import OfficialSiteHarvester
from .metrics import map_metrics, record_baseline
from .pipeline import regrade, sync_profile
from .seed import Lookalike, SeedRow, SeedSummary, import_seed, parse_lookalikes, parse_sweep
from .store import MapStore

MAX_SEED_BYTES = 2_000_000
MAX_HARVEST_DOMAINS = 10


@dataclass(slots=True)
class MapService:
    store: MapStore
    fetcher: PublicPageFetcher | None = None
    engine: MapEngine | None = None

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


    # ------------------------------------------------------- verification
    def sync_profile(self, principal: Principal, institution_id: str, profile: InstitutionProfile | None) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if profile is None:
            raise ValueError("create the intelligence profile (name, location, official domains) first")
        result = sync_profile(self.store, profile)
        result["changes"] = regrade(self.store, institution_id, result["domains_added"] or None)
        return result

    async def harvest(self, principal: Principal, institution_id: str, *, asset_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Harvest official sites for the accounts they declare (at most ten domains per call)."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if self.fetcher is None:
            raise ValueError("page fetching is disabled (GURU_INTELLIGENCE_FETCH_PAGES=false)")
        if asset_ids:
            domains = [asset for asset in (self.store.get_asset(institution_id, asset_id) for asset_id in asset_ids) if asset and asset["kind"] == "domain"]
        else:
            regrade(self.store, institution_id)
            domains = [asset for asset in self.store.list_assets(institution_id, kind="domain", relation="official", limit=500) if asset["grade"] in {"O", "A", "B"}]
        harvester = OfficialSiteHarvester(self.fetcher, self.store)
        run_id = self.store.start_map_run(institution_id, kind="harvest")
        results = []
        for asset in domains[:MAX_HARVEST_DOMAINS]:
            results.append((await harvester.harvest(institution_id, asset["asset_id"], run_id=run_id)).as_dict())
        counts = {"domains": len(results), "accounts": sum(len(item["accounts"]) for item in results), "new_assets": sum(len(item["new_assets"]) for item in results)}
        self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="completed" if len(domains) <= MAX_HARVEST_DOMAINS else "domain_limit", counts=counts)
        return {"run_id": run_id, "results": results, "skipped": max(0, len(domains) - MAX_HARVEST_DOMAINS), **counts}

    def regrade(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        changes = regrade(self.store, institution_id)
        return {"changes": changes, "changed": len(changes)}

    def export(self, principal: Principal, institution_id: str) -> str:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        manager = principal.has_capability(Capability.INTELLIGENCE_MANAGE)
        return export_tsv(self.store, institution_id, min_grade=None if manager else "B")

    # ------------------------------------------------------------ the engine
    async def tick(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if self.engine is None:
            raise ValueError("the map engine is not configured")
        return await self.engine.tick(institution_id)

    def sources(self, principal: Principal, institution_id: str, *, status: str | None = None, connector: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        return self.store.list_sources(institution_id, status=status, connector=connector, limit=limit)

    def connectors(self, principal: Principal, institution_id: str) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        return self.engine.registry.describe() if self.engine else []

    def spend(self, principal: Principal, institution_id: str, *, day: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        caps = dict(self.engine.config.budgets) if self.engine else {}
        share = self.engine.config.tenant_share if self.engine else 1.0
        return {"day": day, "platform": self.store.spend(day=day), "institution": self.store.tenant_spend(institution_id, day=day), "caps": caps, "tenant_caps": {key: value * share for key, value in caps.items()}}


__all__ = ["MAX_HARVEST_DOMAINS", "MAX_SEED_BYTES", "MapService"]
