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
6. corrects any known look-alike that reached B (the canary gate), queues
   what a person must decide (possible impersonators, competing official
   accounts, court records), and records spend, counts, why it stopped and
   the map's metrics.

Free work is claimed before paid work (RSS, re-checks and registries before
paid searches), and a run is charged what it actually spent (a 304 or a
cache hit hands the rest back). Discovery pauses after two passes in a row
whose exploration found nothing new, and resumes as soon as anything new
arrives. Security findings are recorded as soon as the sources have run, so
a later failure cannot lose them.

The run gate: the map's metrics before and after the pass are compared. If
a known look-alike reached B, or holdout recall, seed verification or
precision fell, the pass is held: every grade it changed goes back to what
readers saw and waits as the run's proposal for a manager to publish or
discard. Downgrades that say a site is out of the institution's hands
(compromised, hijacked, parked, redirected, dead) and the canary
corrections themselves are never held back.
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
from .gate import SECURITY_STATUSES, gate_pass, rescore, snapshot
from .grading import SCORER_VERSION
from .incidents import IncidentDesk
from .learning import prioritise_gaps
from .metrics import map_metrics
from .pipeline import nominated, regrade, sync_profile
from .store import MapStore

# What one budget unit costs, relative to the others: work on a free budget is
# claimed before any paid work, and paid work cheapest first.
DEFAULT_UNIT_PRICES: dict[str, float] = {"search": 1.0, "indiankanoon": 1.0}
# How many passes in a row may explore without finding anything before discovery pauses.
DRY_PASSES_TO_PAUSE = 2

DEFAULT_BUDGETS: dict[str, float] = {
    "fetch": 3000, "search": 100, "feed": 2000, "youtube_api": 5000, "wikidata": 300, "crtsh": 100, "rdap": 200, "wayback": 300, "dns": 200, "indiankanoon": 20, "openstreetmap": 100,
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
    unit_prices: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_UNIT_PRICES))


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
    incidents: IncidentDesk | None = None

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
            for domain in self.store.iter_assets(institution_id, kind="domain", relation="official"):
                # Only a domain the institution (or a reviewer or regulator) named is
                # watched as the institution's own site; an inferred one (a subdomain,
                # a Wikidata claim) is not trusted to vouch for accounts.
                if nominated(self.store, institution_id, domain["asset_id"]):
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
        if self.registry.get("search"):
            # Searches where the map has no verified account yet go first.
            prioritise_gaps(self.store, institution_id, now=self.clock())
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

    def _affordable(self, institution_id: str, day: str) -> list[str]:
        """Enabled connectors whose budget still has room today, for this institution and platform-wide."""

        tenant, platform = self.store.tenant_spend(institution_id, day=day), self.store.spend(day=day)
        names = []
        for name in self.registry.names():
            connector = self.registry.get(name)
            if connector is None:
                continue
            try:
                need = max(0.0, min(1.0, float(connector.cost({}))))
            except Exception:  # noqa: BLE001 - a cost that needs a real source is at least one unit
                need = 1.0
            cap = float(self.config.budgets.get(connector.budget_key, 0))
            used_here = float(tenant.get(connector.budget_key, {}).get("units", 0.0))
            used_all = float(platform.get(connector.budget_key, {}).get("units", 0.0))
            if need == 0 or (cap * self.config.tenant_share - used_here >= need and cap - used_all >= need):
                names.append(name)
        return names

    def _price(self, name: str) -> float:
        connector = self.registry.get(name)
        return float(self.config.unit_prices.get(connector.budget_key, 0.0)) if connector is not None else 0.0

    def _claim(self, institution_id: str, worker: str, *, connectors: list[str], total: int, skip_classes: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        """Lease up to ``total`` due sources: free budgets first, then paid ones cheapest first."""

        claimed: list[dict[str, Any]] = []
        tiers: dict[float, list[str]] = {}
        for name in connectors:
            tiers.setdefault(self._price(name), []).append(name)
        for price in sorted(tiers):
            if len(claimed) >= total:
                break
            claimed.extend(self._claim_split(institution_id, worker, connectors=tiers[price], total=total - len(claimed), skip_classes=skip_classes))
        return claimed[:total]

    def _claim_split(self, institution_id: str, worker: str, *, connectors: list[str], total: int, skip_classes: frozenset[str]) -> list[dict[str, Any]]:
        now = self.clock().isoformat()
        claimed: list[dict[str, Any]] = []
        if not connectors or total <= 0:
            return claimed
        classes = [(work_class, share) for work_class, share in self.config.class_split if work_class not in skip_classes]
        for work_class, share in classes:
            quota = max(1, round(total * share))
            # Half of each class goes to gaps and productive sources, half to the
            # longest overdue, so the productive go first and nothing starves.
            first = self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=(quota + 1) // 2, work_class=work_class, connectors=connectors, order="priority")
            rest = self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=quota - len(first), work_class=work_class, connectors=connectors) if quota > len(first) else []
            claimed.extend(first + rest)
        # Slots a class left unused go to whatever else is due (never to a paused class).
        if skip_classes:
            for work_class, _ in classes:
                if len(claimed) >= total:
                    break
                claimed.extend(self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=total - len(claimed), work_class=work_class, connectors=connectors))
        elif len(claimed) < total:
            claimed.extend(self.store.claim_due(institution_id, now=now, worker=worker, lease_seconds=self.config.lease_seconds, limit=total - len(claimed), connectors=connectors))
        for extra in claimed[total:]:
            self.store.release_source(institution_id, extra["source_id"])
        return claimed[:total]

    def _discovery_paused(self, institution_id: str) -> bool:
        """Whether the last passes that explored found nothing new, with nothing new arriving since.

        Newest first: a pass (paused or not) that found new leads or assets
        resumes discovery; otherwise the two most recent passes that ran
        exploration must both have come back empty.
        """

        dry = 0
        for run in self.store.list_map_runs(institution_id, kind="tick", limit=20):
            counts = run.get("counts") or {}
            if run.get("status") != "succeeded":
                continue
            if int(counts.get("explore_run") or 0) == 0:
                if int(counts.get("leads") or 0) or int(counts.get("new_assets") or 0):
                    return False
                continue
            if int(counts.get("explore_found") or 0) > 0:
                return False
            dry += 1
            if dry >= DRY_PASSES_TO_PAUSE:
                return True
        return False

    def _next_interval(self, source: Mapping[str, Any], result: ConnectorResult) -> tuple[int, int]:
        """(seconds until the source is due again, the interval to keep).

        Failure back-off delays only the next run; the kept interval moves
        with yield around the source's base (productive sooner, idle leads
        later, idle watches back to base), so an outage or a quota refusal
        never slows a watch down for good.
        """

        interval = int(source.get("interval_seconds") or 86400)
        base = int(source.get("base_interval_seconds") or interval)
        if result.failed:
            streak = int(source.get("failure_streak") or 0) + 1
            delay = base * (2 ** min(streak, 5))
            bounded = max(self.config.min_interval, min(self.config.max_interval, delay))
            # The kept interval never grows from a failure (one stretched by an older rule comes back to base).
            return bounded, max(self.config.min_interval, min(self.config.max_interval, min(interval, base)))
        if result.yield_count > 0:
            interval = max(interval // 2, base // 8)
        elif source.get("origin") == "lead":
            interval = min(interval * 2, base * 4)
        else:
            interval = base if interval >= base else min(base, interval * 2)
        interval = max(self.config.min_interval, min(self.config.max_interval, interval))
        return interval, interval

    async def _run_source(
        self, institution_id: str, run_id: str, profile: InstitutionProfile | None, connector: Any, source: dict[str, Any], estimate: float,
        counts: dict[str, int], spend: dict[str, float], touched: set[str], incidents: list[dict[str, Any]], review: list[dict[str, Any]], *, day: str | None = None,
    ) -> None:
        try:
            result = await connector.run(source, self._context(institution_id, run_id, profile))
        except Exception as exc:  # noqa: BLE001 - one broken source must not end the tick
            result = ConnectorResult(outcome=f"error:{type(exc).__name__}", failed=True)
        # Charged what the run actually spent: a 304 or a cache hit hands the rest of the reservation back.
        actual = max(0.0, float(result.cost)) if result.cost is not None else estimate
        if day is not None and actual < estimate:
            self.store.refund_budget(institution_id, connector=connector.budget_key, units=estimate - actual, day=day)
        spend[connector.budget_key] = spend.get(connector.budget_key, 0.0) + actual
        counts["sources"] += 1
        counts["new_assets"] += len(result.new_assets)
        counts["raised"] += max(0, result.yield_count - len(result.new_assets))
        counts["failed"] += int(result.failed)
        touched |= result.touched
        incidents.extend({**incident, "connector": connector.name, "source_id": source["source_id"]} for incident in result.incidents)
        review.extend({**item, "connector": connector.name, "source_id": source["source_id"]} for item in result.review)
        added = 0
        for lead in result.leads:
            added += int(self._add_lead(institution_id, lead))
        counts["leads"] += added
        if source.get("work_class") == "explore":
            counts["explore_run"] = counts.get("explore_run", 0) + 1
            counts["explore_found"] = counts.get("explore_found", 0) + result.yield_count + added
        delay, keep = self._next_interval(source, result)
        promoted = source.get("origin") == "lead" and result.yield_count > 0
        counts["pruned"] += int(result.prune)
        self.store.complete_source(
            institution_id, source["source_id"], outcome=result.outcome, next_due=(self.clock() + timedelta(seconds=delay)).isoformat(), interval_seconds=keep, yield_count=result.yield_count,
            cost=actual, failed=result.failed, etag=result.etag, last_modified=result.last_modified, status="pruned" if result.prune else None,
            # A lead that paid off becomes a regular watch, so pausing discovery never pauses it.
            origin="recurring" if promoted else None, clear_expiry=promoted, work_class="rotation" if promoted else None,
        )

    # ------------------------------------------------------------------ tick
    async def tick(self, institution_id: str, *, worker: str | None = None) -> dict[str, Any]:
        worker = worker or f"tick-{uuid4().hex[:8]}"
        run_id = self.store.try_start_map_run(institution_id, kind="tick", lock_seconds=self.config.lock_seconds)
        if run_id is None:
            return {"institution_id": institution_id, "run_id": None, "skipped": "another tick is in progress for this institution"}
        counts = {
            "sources": 0, "new_assets": 0, "raised": 0, "leads": 0, "deferred_budget": 0, "failed": 0, "pruned": 0, "incidents": 0, "review": 0, "expired": 0, "watches_added": 0,
            "explore_run": 0, "explore_found": 0, "discovery_paused": 0, "held": 0,
        }
        spend: dict[str, float] = {}
        incidents: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        recorded = 0

        def record_incidents() -> None:
            # Recorded as soon as they are known, so a later failure in the pass cannot lose them.
            nonlocal recorded
            fresh = incidents[recorded:]
            recorded = len(incidents)
            if self.incidents is not None and fresh:
                counts["incidents_new"] = counts.get("incidents_new", 0) + self.incidents.record(institution_id, fresh, run_id=run_id)["new"]

        try:
            profile = self.profile_loader(institution_id) if self.profile_loader else None
            counts["watches_added"] = self.ensure_recurring(institution_id, profile)
            counts["expired"] = self.store.expire_sources(institution_id, now=self.clock().isoformat())
            if self.store.has_stale_scores(institution_id, SCORER_VERSION):
                # A new scoring rule reaches readers only through the gate.
                counts["rescored"] = int(rescore(self.store, institution_id, run_id=f"rescore:{SCORER_VERSION}").passed)
            before = map_metrics(self.store, institution_id)
            published = snapshot(self.store, institution_id)
            paused = self._discovery_paused(institution_id)
            counts["discovery_paused"] = int(paused)
            skip = frozenset({"explore"}) if paused else frozenset()
            day = self.clock().date().isoformat()
            touched: set[str] = set()
            # Only connectors whose budget has room are claimed; when a budget
            # runs out mid-tick its connectors drop out and the freed slots go
            # to due work on other budgets (deferred sources keep their place).
            affordable = self._affordable(institution_id, day)
            slots = max(1, self.config.sources_per_tick)
            batches = 0
            while slots > counts["sources"] and affordable and batches < 5:
                batches += 1
                batch = self._claim(institution_id, worker, connectors=affordable, total=slots - counts["sources"], skip_classes=skip)
                if not batch:
                    break
                deferred_before = counts["deferred_budget"]
                for source in batch:
                    connector = self.registry.get(source["connector"])
                    if connector is None or connector.name not in affordable:
                        self.store.release_source(institution_id, source["source_id"])
                        continue
                    estimate = float(connector.cost(source))
                    cap = float(self.config.budgets.get(connector.budget_key, 0))
                    if estimate > 0 and not self.store.reserve_budget(institution_id, connector=connector.budget_key, units=estimate, day=day, global_cap=cap, tenant_cap=cap * self.config.tenant_share):
                        counts["deferred_budget"] += 1
                        self.store.release_source(institution_id, source["source_id"])
                        spent_key = connector.budget_key
                        affordable = [name for name in affordable if (self.registry.get(name) and self.registry.get(name).budget_key != spent_key)]
                        continue
                    await self._run_source(institution_id, run_id, profile, connector, source, estimate, counts, spend, touched, incidents, review, day=day)
                if counts["deferred_budget"] == deferred_before:
                    break  # claim again only to refill slots a spent budget left empty
            record_incidents()
            if touched:
                regrade(self.store, institution_id, sorted(touched))
                review.extend(self._disputes(institution_id, touched))
            # Ground truth last: a known look-alike that reached B is set back and
            # reported, and a pass that regressed is held for a manager.
            gated = gate_pass(self.store, institution_id, before=before, published=published, run_id=run_id)
            incidents.extend(gated["leaks"])
            counts["canary_leaks_caught"] = len(gated["leaks"])
            counts["held"] = gated["held"]
            record_incidents()
            reasons = gated["reasons"]
            if gated["review"]:
                review.append(gated["review"])
            self.store.expire_review_items(institution_id, now=self.clock().isoformat())
            queued = [self._queue(institution_id, item, run_id) for item in review]
            counts["incidents"] = len(incidents)
            counts["review"] = queued.count("added")
            counts["review_full"] = queued.count("full")
            stop_reason = "budget" if counts["deferred_budget"] else ("dry" if counts["sources"] and not (counts["new_assets"] or counts["raised"]) else ("idle" if not counts["sources"] else "completed"))
            if paused and stop_reason in {"dry", "idle"}:
                stop_reason = "discovery_paused"
            metrics = map_metrics(self.store, institution_id)
            self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason=stop_reason, gate="held" if reasons else "passed", spend=spend, counts=counts, metrics=metrics)
        except Exception as exc:
            try:
                record_incidents()
            except Exception:  # noqa: BLE001 - the original failure is the one to report
                pass
            self.store.finish_map_run(institution_id, run_id, status="failed", counts=counts, spend=spend, error=str(exc)[:300])
            raise
        return {
            "institution_id": institution_id, "run_id": run_id, "stop_reason": stop_reason, "gate": "held" if reasons else "passed", "counts": counts, "spend": spend, "incidents": incidents, "review": review,
            "metrics": {key: metrics[key] for key in ("assets", "verified", "holdout_recall", "canary_leaks")},
        }

    def _queue(self, institution_id: str, item: Mapping[str, Any], run_id: str) -> str:
        _, outcome = self.store.add_review_item(
            institution_id, kind=str(item["kind"]), title=str(item.get("title") or item["kind"]), detail=str(item.get("detail") or ""), url=str(item.get("url") or ""),
            asset_id=item.get("asset_id"), entity_id=item.get("entity_id"), severity=str(item.get("severity") or "normal"), connector=str(item.get("connector") or ""),
            source_id=item.get("source_id"), run_id=run_id, fingerprint=item.get("fingerprint"),
        )
        return outcome

    def _disputes(self, institution_id: str, touched: set[str]) -> list[dict[str, Any]]:
        """Competing unanchored 'official' accounts need a person to say which is real."""

        return [
            {"kind": "dispute", "asset_id": asset["asset_id"], "entity_id": asset["entity_id"], "title": f"More than one account claims to be official on {asset['platform']}: {asset['handle']}", "url": asset["url"]}
            for asset in self.store.get_assets(institution_id, touched).values() if asset["status"] == "disputed"
        ]

    async def tick_all(self, institutions: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for institution_id in institutions:
            try:
                results.append(await self.tick(institution_id))
            except Exception as exc:  # noqa: BLE001
                results.append({"institution_id": institution_id, "error": str(exc)[:300]})
        return results


__all__ = ["DEFAULT_BUDGETS", "DEFAULT_UNIT_PRICES", "DRY_PASSES_TO_PAUSE", "EngineConfig", "MapEngine", "SECURITY_STATUSES"]
