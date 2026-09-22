"""The map engine: one scheduled tick per institution that picks up where the last one stopped.

Every tick:

1. takes a per-institution run lock (a tick that overlaps another is skipped);
2. makes sure the recurring watches exist (each official domain, fetchable
   pages to re-check, and whatever the registered connectors ask for) and
   expires leads that never produced anything;
3. leases due sources, split by work class: rotation (the recurring
   watches), re-checks, and exploration of new leads, 60/20/20 by default,
   with unused slots flowing to the other classes;
4. runs each through its connector after reserving budget (the tenant's
   quota and the platform-wide cap; work that does not fit is deferred,
   never dropped);
5. turns what a connector found into new sources (respecting the hop cap
   and suppression), reschedules each source from its yield (productive
   sources come back sooner, idle ones later, failing ones back off), and
6. records spend, counts, why it stopped and the map's metrics.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..fetch import PublicPageFetcher
from ..profile import InstitutionProfile
from .connectors.base import ConnectorContext, ConnectorRegistry, ConnectorResult, Lead
from .metrics import map_metrics
from .pipeline import regrade, sync_profile
from .store import MapStore

DEFAULT_BUDGETS: dict[str, float] = {
    "fetch": 3000, "search": 100, "feed": 2000, "youtube_api": 5000, "wikidata": 300, "crtsh": 100, "rdap": 200, "wayback": 300, "dns": 200, "indiankanoon": 20,
}


@dataclass(slots=True)
class EngineConfig:
    sources_per_tick: int = 25
    lease_seconds: int = 900
    lock_seconds: int = 3600
    max_hops: int = 2
    lead_ttl_days: int = 30
    min_interval: int = 3600
    max_interval: int = 30 * 86400
    class_split: tuple[tuple[str, float], ...] = (("rotation", 0.6), ("recheck", 0.2), ("explore", 0.2))
    budgets: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    tenant_share: float = 0.5


@dataclass(slots=True)
class MapEngine:
    store: MapStore
    registry: ConnectorRegistry
    config: EngineConfig = field(default_factory=EngineConfig)
    fetcher: PublicPageFetcher | None = None
    search: Any | None = None
    profile_loader: Callable[[str], InstitutionProfile | None] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    extras: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ scheduling
    def ensure_recurring(self, institution_id: str, profile: InstitutionProfile | None) -> int:
        """Make sure every standing watch exists; returns how many were added."""

        added = 0
        if profile is not None:
            sync_profile(self.store, profile)
        # A domain that was never graded (new from the profile or a seed) is
        # graded now, so a configured domain is watched from the first tick.
        unrated = [domain["asset_id"] for domain in self.store.iter_assets(institution_id, kind="domain") if domain["grade"] == "unrated"]
        if unrated:
            regrade(self.store, institution_id, unrated)
        now = self.clock().isoformat()
        if self.registry.get("official_site"):
            for domain in self.store.iter_assets(institution_id, kind="domain"):
                if domain["relation"] == "official" and domain["grade"] in {"O", "A", "B"} and domain["status"] not in {"parked", "hijacked"}:
                    _, created = self.store.upsert_source(institution_id, connector="official_site", target=domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], origin="recurring", work_class="rotation", interval_seconds=7 * 86400, due_at=now)
                    added += int(created)
        if self.registry.get("recheck"):
            for page in self.store.iter_assets(institution_id, kind="page"):
                if page["platform"] == "website":
                    _, created = self.store.upsert_source(institution_id, connector="recheck", target=page["asset_id"], entity_id=page["entity_id"], asset_id=page["asset_id"], origin="recurring", work_class="recheck", interval_seconds=14 * 86400, due_at=now)
                    added += int(created)
        for name in self.registry.names():
            connector = self.registry.get(name)
            plan = getattr(connector, "plan", None)
            if callable(plan):
                for lead in plan(self._context(institution_id, "planning", profile)):
                    added += int(self._add_lead(institution_id, lead, origin_override=lead.origin))
        return added

    def _context(self, institution_id: str, run_id: str, profile: InstitutionProfile | None) -> ConnectorContext:
        return ConnectorContext(self.store, institution_id, run_id, self.clock(), fetcher=self.fetcher, search=self.search, profile=profile, max_hops=self.config.max_hops, extras=self.extras)

    def _add_lead(self, institution_id: str, lead: Lead, *, origin_override: str | None = None) -> bool:
        if lead.hops > self.config.max_hops:
            return False
        if self.store.is_suppressed(institution_id, lead.target):
            return False
        origin = origin_override or lead.origin
        expires = None if origin in {"recurring", "profile", "gap", "seed"} else (self.clock() + timedelta(days=self.config.lead_ttl_days)).isoformat()
        _, created = self.store.upsert_source(
            institution_id, connector=lead.connector, target=lead.target, entity_id=lead.entity_id, asset_id=lead.asset_id, topic=lead.topic, origin=origin,
            work_class=lead.work_class, hops=lead.hops, interval_seconds=lead.interval_seconds, due_at=self.clock().isoformat(), expires_at=expires,
        )
        return created

    def _claim(self, institution_id: str, worker: str) -> list[dict[str, Any]]:
        now = self.clock().isoformat()
        enabled = self.registry.names()
        total = max(1, self.config.sources_per_tick)
        claimed: list[dict[str, Any]] = []
        for work_class, share in self.config.class_split:
            quota = max(1, round(total * share))
            claimed.extend(self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=quota, work_class=work_class, connectors=enabled))
        if len(claimed) < total:
            # Slots a class left unused go to whatever else is due.
            claimed.extend(self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=total - len(claimed), connectors=enabled))
        return claimed[:total]

    def _next_interval(self, source: Mapping[str, Any], result: ConnectorResult) -> int:
        interval = int(source.get("interval_seconds") or 86400)
        if result.failed:
            streak = int(source.get("failure_streak") or 0) + 1
            interval = interval * (2 ** min(streak, 5))
        elif result.yield_count > 0:
            interval = interval // 2
        elif source.get("origin") == "lead":
            interval = interval * 2
        return max(self.config.min_interval, min(self.config.max_interval, interval))

    # ------------------------------------------------------------------ tick
    async def tick(self, institution_id: str, *, worker: str | None = None) -> dict[str, Any]:
        worker = worker or f"tick-{uuid4().hex[:8]}"
        run_id = self.store.try_start_map_run(institution_id, kind="tick", lock_seconds=self.config.lock_seconds)
        if run_id is None:
            return {"institution_id": institution_id, "run_id": None, "skipped": "another tick is in progress for this institution"}
        counts = {"sources": 0, "new_assets": 0, "raised": 0, "leads": 0, "deferred_budget": 0, "failed": 0, "pruned": 0, "incidents": 0, "review": 0, "expired": 0, "watches_added": 0}
        spend: dict[str, float] = {}
        incidents: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        try:
            profile = self.profile_loader(institution_id) if self.profile_loader else None
            counts["watches_added"] = self.ensure_recurring(institution_id, profile)
            counts["expired"] = self.store.expire_sources(institution_id, now=self.clock().isoformat())
            day = self.clock().date().isoformat()
            touched: set[str] = set()
            for source in self._claim(institution_id, worker):
                connector = self.registry.get(source["connector"])
                if connector is None:
                    self.store.release_source(institution_id, source["source_id"])
                    continue
                estimate = float(connector.cost(source))
                cap = float(self.config.budgets.get(connector.budget_key, 0))
                if estimate > 0 and not self.store.reserve_budget(institution_id, connector=connector.budget_key, units=estimate, day=day, global_cap=cap, tenant_cap=cap * self.config.tenant_share):
                    counts["deferred_budget"] += 1
                    self.store.release_source(institution_id, source["source_id"])
                    continue
                spend[connector.budget_key] = spend.get(connector.budget_key, 0.0) + estimate
                try:
                    result = await connector.run(source, self._context(institution_id, run_id, profile))
                except Exception as exc:  # noqa: BLE001 - one broken source must not end the tick
                    result = ConnectorResult(outcome=f"error:{type(exc).__name__}", failed=True)
                counts["sources"] += 1
                counts["new_assets"] += len(result.new_assets)
                counts["raised"] += max(0, result.yield_count - len(result.new_assets))
                counts["failed"] += int(result.failed)
                touched |= result.touched
                incidents.extend({**incident, "connector": connector.name, "source_id": source["source_id"]} for incident in result.incidents)
                review.extend({**item, "connector": connector.name, "source_id": source["source_id"]} for item in result.review)
                for lead in result.leads:
                    counts["leads"] += int(self._add_lead(institution_id, lead))
                next_due = (self.clock() + timedelta(seconds=self._next_interval(source, result))).isoformat()
                promoted = source.get("origin") == "lead" and result.yield_count > 0
                status = "pruned" if result.prune else None
                counts["pruned"] += int(result.prune)
                self.store.complete_source(
                    institution_id, source["source_id"], outcome=result.outcome, next_due=next_due, interval_seconds=self._next_interval(source, result), yield_count=result.yield_count,
                    cost=result.cost if result.cost is not None else estimate, failed=result.failed, etag=result.etag, last_modified=result.last_modified, status=status,
                    origin="recurring" if promoted else None, clear_expiry=promoted,
                )
            if touched:
                regrade(self.store, institution_id, sorted(touched))
            counts["incidents"] = len(incidents)
            counts["review"] = len(review)
            stop_reason = "budget" if counts["deferred_budget"] else ("dry" if counts["sources"] and not (counts["new_assets"] or counts["raised"]) else ("idle" if not counts["sources"] else "completed"))
            metrics = map_metrics(self.store, institution_id)
            self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason=stop_reason, spend=spend, counts=counts, metrics=metrics)
        except Exception as exc:
            self.store.finish_map_run(institution_id, run_id, status="failed", counts=counts, spend=spend, error=str(exc)[:300])
            raise
        return {
            "institution_id": institution_id, "run_id": run_id, "stop_reason": stop_reason, "counts": counts, "spend": spend, "incidents": incidents, "review": review,
            "metrics": {key: metrics[key] for key in ("assets", "verified", "holdout_recall", "canary_leaks")},
        }

    async def tick_all(self, institutions: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for institution_id in institutions:
            try:
                results.append(await self.tick(institution_id))
            except Exception as exc:  # noqa: BLE001
                results.append({"institution_id": institution_id, "error": str(exc)[:300]})
        return results


__all__ = ["DEFAULT_BUDGETS", "EngineConfig", "MapEngine"]
