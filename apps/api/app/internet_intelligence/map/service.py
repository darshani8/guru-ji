"""Guarded entry points to the internet map for routes, agents and scripts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ...domain.principals import Capability, InstitutionScope, Principal
from ..fetch import PublicPageFetcher
from ..profile import InstitutionProfile
from .engine import MapEngine
from .export import export_tsv
from .gate import gate_pass, guard_canaries, rescore, snapshot
from .assets import asset_ref
from .connectors.base import ConnectorContext
from .incidents import IncidentDesk
from .learning import coverage_estimate, gap_grid
from .ownership import OwnerClaimsConnector, instructions, owned_domains
from .harvest import OfficialSiteHarvester
from .metrics import map_metrics, record_baseline, run_series
from .pipeline import nominated, regrade, sync_profile
from .seed import DEFAULT_SWEEP, Lookalike, SeedRow, SeedSummary, import_seed, parse_lookalikes, parse_sweep
from .store import MapStore

MAX_SEED_BYTES = 2_000_000
# The bundled sweep has about 440 rows; a larger file is split into several imports.
MAX_SEED_ROWS = 5_000
MAX_HARVEST_DOMAINS = 10
MANUAL_SOURCE_CONNECTORS = frozenset({"directory", "lead_page", "feed"})
# What a person may decide about each kind of review item.
_ON_ASSETS = frozenset({"confirm", "reject", "lookalike", "impersonation", "personal", "dismiss"})
DECISIONS: dict[str, frozenset[str]] = {
    "impersonation_candidate": _ON_ASSETS, "dispute": _ON_ASSETS, "candidate_account": _ON_ASSETS, "canary_leak": frozenset({"acknowledge", "dismiss"}),
    "court_record": frozenset({"acknowledge", "dismiss"}), "run_gate": frozenset({"publish", "discard"}),
}
ASSET_DECISIONS = frozenset({"confirm", "reject", "lookalike", "impersonation", "personal"})
# The summary's grid is cut here for display (and says so); the engine searches every entity.
SUMMARY_GRID_ENTITIES = 300


def _shown(metrics: Mapping[str, Any], *, manager: bool) -> dict[str, Any]:
    """The measures anyone who may read the map sees; grades below B and the engine's backlog are for managers."""

    return {
        "assets": metrics["assets"], "verified": metrics["verified"], "by_platform": metrics["by_platform"],
        "by_grade": {grade: count for grade, count in metrics["by_grade"].items() if manager or grade in {"O", "A", "A-arch", "B"}},
        "freshness": {key: value for key, value in metrics["freshness"].items() if manager or key != "sources_overdue"},
    }


@dataclass(slots=True)
class MapService:
    store: MapStore
    fetcher: PublicPageFetcher | None = None
    engine: MapEngine | None = None
    # institution -> the sweep groups that are its own (operator configuration)
    seed_groups: Mapping[str, Sequence[str]] = field(default_factory=dict)
    desk: IncidentDesk | None = None

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
            # rows, and grades a gate has not published, are working material
            # for the people who manage it.
            assets = [
                {key: value for key, value in asset.items() if not key.startswith("proposed_")}
                for asset in assets if asset["grade"] in {"O", "A", "A-arch", "B"} or asset["relation"] == "community" and asset["grade"] == "C"
            ]
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

    def metrics(self, principal: Principal, institution_id: str, *, kind: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Readers get what the summary shows them; the ground truth and the runs (their spend, who approved an import) are for managers."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        metrics = map_metrics(self.store, institution_id)
        if not principal.has_capability(Capability.INTELLIGENCE_MANAGE):
            return _shown(metrics, manager=False)
        return {**metrics, "runs": self.store.list_map_runs(institution_id, kind=kind, limit=limit)}

    # --------------------------------------------------------------- seeding
    def seed(
        self, principal: Principal, institution_id: str, *, sweep_text: str, lookalikes_text: str = "", groups: Sequence[str] | None = None, all_groups: bool = False,
        approved_by: str | None = None, holdout_percent: int = 20, source: str = "sweep",
    ) -> dict[str, Any]:
        """Import a sweep. Anything beyond the institution's own groups needs a named approval.

        Which sweep groups belong to an institution is operator configuration
        (GURU_INTELLIGENCE_SEED_GROUPS), never the caller's say-so: a group
        label in a request, or in a sweep file the caller wrote, proves
        nothing. An import of the bundled sweep limited to the institution's
        configured groups needs no approval; anything else (other groups,
        every group, a caller-supplied sweep) needs ``approved_by``, which is
        recorded with the importing principal in the baseline run.
        """

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if len(sweep_text.encode("utf-8")) + len(lookalikes_text.encode("utf-8")) > MAX_SEED_BYTES:
            raise ValueError("the seed files are too large")
        if not all_groups and not groups:
            raise ValueError("name the groups to import, or set all_groups with approved_by")
        rows: list[SeedRow] = parse_sweep(sweep_text)
        if len(rows) > MAX_SEED_ROWS:
            raise ValueError(f"a seed import takes at most {MAX_SEED_ROWS} rows; split the file")
        requested = {row.group.strip().lower() for row in rows} if all_groups else {group.strip().lower() for group in groups or ()}
        own = {group.strip().lower() for group in self.seed_groups.get(institution_id, ())}
        bundled = sweep_text == DEFAULT_SWEEP.read_text(encoding="utf-8")
        needs_approval = not bundled or not requested or not requested <= own
        approver = (approved_by or "").strip()
        if needs_approval and not approver:
            raise ValueError(
                "this import maps groups outside the institution's own (" + (", ".join(sorted(own)) or "none configured") + "); it needs approved_by: who approved mapping them"
            )
        lookalikes: list[Lookalike] = parse_lookalikes(lookalikes_text) if lookalikes_text.strip() else []
        with self.store.batch(institution_id):
            summary: SeedSummary = import_seed(self.store, institution_id, rows, lookalikes=lookalikes, groups=None if all_groups else groups, holdout_percent=holdout_percent, source=source)
        record = {"imported_by": principal.principal_id, "groups": summary.groups, "all_groups": all_groups, "approved_by": approver or None, "needed_approval": needs_approval, "source": source}
        baseline = record_baseline(self.store, institution_id, import_record=record)
        return {"summary": summary.as_dict(), "baseline": baseline, "approved_by": approver or None, "needed_approval": needs_approval}

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
        # A harvest a manager starts goes through the same gate as a scheduled pass.
        before, published = map_metrics(self.store, institution_id), snapshot(self.store, institution_id)
        results = []
        for asset in domains[:MAX_HARVEST_DOMAINS]:
            results.append((await harvester.harvest(institution_id, asset["asset_id"], run_id=run_id)).as_dict())
        gated = gate_pass(self.store, institution_id, before=before, published=published, run_id=run_id, label="harvest")
        if gated["review"]:
            self.store.add_review_item(institution_id, run_id=run_id, **gated["review"])
        found = [incident for item in results for incident in item["incidents"]] + gated["leaks"]
        if found and self.desk is not None:
            # A hacked or hijacked site found now is recorded (and alerted) now, not at the next weekly pass.
            self.desk.record(institution_id, found, run_id=run_id)
        counts = {"domains": len(results), "accounts": sum(len(item["accounts"]) for item in results), "new_assets": sum(len(item["new_assets"]) for item in results), "held": gated["held"]}
        self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="completed" if len(domains) <= MAX_HARVEST_DOMAINS else "domain_limit", gate="held" if gated["reasons"] else "passed", counts=counts)
        return {"run_id": run_id, "results": results, "skipped": max(0, len(domains) - MAX_HARVEST_DOMAINS), "gate": {"held": bool(gated["reasons"]), "reasons": gated["reasons"]}, **counts}

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

    def add_source(self, principal: Principal, institution_id: str, *, connector: str, target: str) -> dict[str, Any]:
        """A manager points a page-reading connector at a URL (a regulator's listing, a directory page)."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if connector not in MANUAL_SOURCE_CONNECTORS:
            raise ValueError(f"sources can be added by hand only for {', '.join(sorted(MANUAL_SOURCE_CONNECTORS))}")
        parsed = urlparse(target.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or len(target) > 1000:
            raise ValueError("the target must be a public http(s) URL")
        if self.store.is_suppressed(institution_id, target.strip()):
            raise ValueError("that address is suppressed for this institution")
        source_id, created = self.store.upsert_source(institution_id, connector=connector, target=target.strip(), origin="seed", work_class="explore" if connector == "lead_page" else "rotation", hops=0, interval_seconds=30 * 86400)
        return {"source_id": source_id, "created": created, "enabled": bool(self.engine and self.engine.registry.get(connector))}

    # ------------------------------------------------------------ review queue
    def review_items(self, principal: Principal, institution_id: str, *, status: str | None = "open", kind: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """The queue is manager-only: it holds unverified accounts, suspected impersonators and court records."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        return {"items": self.store.list_review_items(institution_id, status=status, kind=kind, limit=limit, offset=offset), "open": self.store.review_counts(institution_id)}

    def decide(self, principal: Principal, institution_id: str, review_id: str, *, decision: str, note: str = "", relation: str | None = None) -> dict[str, Any]:
        """Apply a person's decision. Every effect on a grade goes through evidence, so it can be explained later."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        item = self.store.get_review_item(institution_id, review_id)
        if item is None:
            raise KeyError("review item not found")
        if item["status"] != "open":
            raise ValueError(f"this item is already {item['status']}")
        allowed = DECISIONS.get(item["kind"], frozenset())
        if decision not in allowed:
            raise ValueError(f"a {item['kind']} item takes one of: {', '.join(sorted(allowed))}")
        asset = self.store.get_asset(institution_id, item["asset_id"]) if item.get("asset_id") else None
        if decision in ASSET_DECISIONS and asset is None:
            raise ValueError("the asset this item was about is no longer in the map")
        effect: dict[str, Any] = {"decision": decision}
        reviewer = f"reviewer:{principal.principal_id}"
        detail = (note or decision).strip()[:300]
        if decision == "confirm":
            self.store.add_evidence(institution_id, asset_id=asset["asset_id"], kind="reviewer_confirm", detail=detail, channel=reviewer, observed_via="reviewer")
            if relation:
                self.store.set_relation(institution_id, asset["asset_id"], relation)
        elif decision in {"reject", "lookalike", "impersonation"}:
            kind = {"reject": "reviewer_reject", "lookalike": "lookalike", "impersonation": "impersonation"}[decision]
            self.store.add_evidence(institution_id, asset_id=asset["asset_id"], kind=kind, polarity="refutes", detail=detail, channel=reviewer, observed_via="reviewer")
            # A rejected website takes its host's leads with it, unless it is one of ours.
            whole_host = asset["kind"] == "domain" and asset["platform"] == "website" and not nominated(self.store, institution_id, asset["asset_id"])
            effect["sources_pruned"] = self.store.prune_sources_for(institution_id, asset_id=asset["asset_id"], url=asset["url"], whole_host=whole_host)
            if decision == "impersonation":
                effect["incident"] = {"kind": "impersonation_confirmed", "target": asset["asset_key"], "signals": [detail], "severity": "high"}
                if self.desk is not None:
                    self.desk.record(institution_id, [effect["incident"]])
        elif decision == "personal":
            # A person's account leaves the map: only a keyed fingerprint stays, so it never comes back.
            self.store.suppress(institution_id, asset["asset_key"], reason="personal account (review decision)")
            self.store.suppress(institution_id, asset["url"], reason="personal account (review decision)")
            self.store.forget_asset(institution_id, asset["asset_id"], keep_review_id=review_id)
            asset = None
        elif decision == "publish":
            # Only the proposal of the run this item is about, and the canary guard runs on what went live.
            effect["published"] = self.store.apply_proposed(institution_id, run_id=item.get("run_id"))
            effect["canary_leaks_corrected"] = len(guard_canaries(self.store, institution_id, run_id=item.get("run_id")))
        elif decision == "discard":
            effect["discarded"] = self.store.discard_proposed(institution_id, run_id=item.get("run_id"))
        if asset is not None and decision in ASSET_DECISIONS:
            effect["changes"] = regrade(self.store, institution_id, [asset["asset_id"]])
        if not self.store.decide_review_item(institution_id, review_id, decision=decision, decided_by=principal.principal_id, note=note, redact=decision == "personal"):
            raise ValueError("this item was decided by someone else just now")
        return {"review_id": review_id, "kind": item["kind"], **effect}

    def suppress(self, principal: Principal, institution_id: str, *, identifier: str, reason: str) -> dict[str, Any]:
        """Keep an account or address out of the map for good (a person's account, an opt-out request).

        Only a keyed fingerprint is stored; if the map already holds the
        account it is removed with its evidence.
        """

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        value = identifier.strip()
        if not value or len(value) > 1000:
            raise ValueError("name the account URL or address to suppress")
        keys = [value]
        try:
            ref = asset_ref(value)
            keys = [ref.key, ref.url, value]
        except ValueError:
            ref = None
        for key in dict.fromkeys(keys):
            self.store.suppress(institution_id, key, reason=reason[:200])
        existing = self.store.find_asset(institution_id, ref.key) if ref else None
        removed = bool(existing) and self.store.forget_asset(institution_id, existing["asset_id"])
        return {"suppressed": True, "removed": removed}

    # ----------------------------------------------------------------- summary
    def summary(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        """One view of the map: what it stands behind, where it has gaps, how complete it probably is.

        Readers see grades of B and better only (a cell below that just shows
        as not yet covered) and how fresh those are; managers also see the
        ground truth, incidents, the review queue, runs (and the series of
        ticks, each with its recall change, look-alikes caught and cost),
        spend and what each connector yields. The grid lists the institution's
        own entities first and says when it is cut short.
        """

        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        manager = principal.has_capability(Capability.INTELLIGENCE_MANAGE)
        metrics = map_metrics(self.store, institution_id)
        grid = gap_grid(self.store, institution_id, max_entities=SUMMARY_GRID_ENTITIES, own_groups=self.seed_groups.get(institution_id, ()))
        if not manager:
            for row in grid["rows"]:
                for cell in row["cells"].values():
                    if not cell["covered"]:
                        cell["grade"] = None
        result: dict[str, Any] = {
            "institution_id": institution_id, **_shown(metrics, manager=manager),
            "grid": {key: value for key, value in grid.items() if key != "gaps"}, "coverage": coverage_estimate(self.store, institution_id),
        }
        if manager:
            today = datetime.now(timezone.utc).date().isoformat()
            connectors = self.store.source_yields(institution_id)
            # Budget units (requests, or a provider's quota units) every source has used so far, over what the map now stands behind.
            cost_total = round(sum(float(row["cost"] or 0) for row in connectors), 2)
            result.update({
                "ground_truth": {key: metrics[key] for key in ("holdout_recall", "holdout_total", "precision", "seed_verification_rate", "seed_verified", "seed_total", "canary_total", "canary_leaks")},
                "incidents_open": [{key: row[key] for key in ("incident_id", "kind", "target", "severity", "status", "times_seen", "last_seen_at")} for row in self.store.list_incidents(institution_id, limit=50) if row["status"] != "resolved"],
                "review_waiting": self.store.review_counts(institution_id), "runs": self.store.list_map_runs(institution_id, limit=5), "series": run_series(self.store, institution_id),
                "cost": {"cost_total": cost_total, "verified": metrics["verified"], "cost_per_verified": round(cost_total / metrics["verified"], 2) if metrics["verified"] else None},
                "connectors": connectors, "spend_today": self.store.tenant_spend(institution_id, day=today),
            })
        return result

    # --------------------------------------------------------------- ownership
    def ownership(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        """The institution's token, how to publish it, and what the owner has confirmed so far."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        domains = [domain["asset_key"].removeprefix("web:") for domain in owned_domains(self.store, institution_id)]
        confirmed = [{key: asset[key] for key in ("asset_id", "asset_key", "url", "kind", "platform")} for asset in self.store.iter_assets(institution_id, grade="O")]
        return {**instructions(self.store.suppression_key, institution_id, domains), "confirmed": confirmed}

    async def verify_ownership(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        """Check every official domain for the token now (the engine also does this weekly)."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if self.fetcher is None:
            raise ValueError("page fetching is disabled (GURU_INTELLIGENCE_FETCH_PAGES=false)")
        connector = self.engine.registry.get("owner_claims") if self.engine else None
        connector = connector or OwnerClaimsConnector(key=self.store.suppression_key)
        run_id = self.store.start_map_run(institution_id, kind="ownership")
        context = ConnectorContext(self.store, institution_id, run_id, datetime.now(timezone.utc), fetcher=self.fetcher)
        results = []
        for domain in owned_domains(self.store, institution_id)[:MAX_HARVEST_DOMAINS]:
            outcome = await connector.run({"target": domain["asset_id"]}, context)
            results.append({"domain": domain["asset_key"].removeprefix("web:"), "outcome": outcome.outcome, "accounts_added": outcome.new_assets})
        self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="completed", counts={"domains": len(results), "verified": sum(item["outcome"] == "verified" for item in results)})
        return {"run_id": run_id, "results": results, **self.ownership(principal, institution_id)}

    # --------------------------------------------------------------- incidents
    def incidents(self, principal: Principal, institution_id: str, *, status: str | None = None, severity: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        return self.store.list_incidents(institution_id, status=status, severity=severity, limit=limit)

    def set_incident(self, principal: Principal, institution_id: str, incident_id: str, *, status: str, note: str = "") -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        if self.store.get_incident(institution_id, incident_id) is None:
            raise KeyError("incident not found")
        if not self.store.set_incident_status(institution_id, incident_id, status=status, by=principal.principal_id, note=note):
            raise ValueError("this incident is already resolved; it reopens by itself if it is seen again")
        return self.store.get_incident(institution_id, incident_id) or {}

    def digest(self, principal: Principal, institution_id: str, *, send: bool = False) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        desk = self.desk or IncidentDesk(self.store)
        return desk.send_digest(institution_id) if send else {**desk.digest(institution_id), "sent": False}

    def rescore(self, principal: Principal, institution_id: str) -> dict[str, Any]:
        """Recompute every grade under the current rule; publish only if the ground truth does not get worse."""

        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        run_id = self.store.start_map_run(institution_id, kind="rescore")
        verdict = rescore(self.store, institution_id, run_id=run_id)
        self.store.finish_map_run(institution_id, run_id, status="succeeded", stop_reason="published" if verdict.passed else "held", gate="passed" if verdict.passed else "held", metrics=verdict.after)
        return {"run_id": run_id, **verdict.as_dict()}

    def connectors(self, principal: Principal, institution_id: str) -> list[dict[str, Any]]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_READ)
        return self.engine.registry.describe() if self.engine else []

    def spend(self, principal: Principal, institution_id: str, *, day: str) -> dict[str, Any]:
        self.guard(principal, institution_id, Capability.INTELLIGENCE_MANAGE)
        caps = dict(self.engine.config.budgets) if self.engine else {}
        share = self.engine.config.tenant_share if self.engine else 1.0
        return {"day": day, "platform": self.store.spend(day=day), "institution": self.store.tenant_spend(institution_id, day=day), "caps": caps, "tenant_caps": {key: value * share for key, value in caps.items()}}


__all__ = ["ASSET_DECISIONS", "DECISIONS", "MANUAL_SOURCE_CONNECTORS", "MAX_HARVEST_DOMAINS", "MAX_SEED_BYTES", "MAX_SEED_ROWS", "MapService"]
