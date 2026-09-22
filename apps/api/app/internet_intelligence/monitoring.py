"""Continuous monitoring: scheduled searches, change detection, alerts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .query_generator import DEFAULT_TOPICS
from .service import InternetIntelligenceService
from .store import RUN_LOCK_SECONDS, IntelligenceStore

AlertSink = Callable[[str, Sequence[str], str, str], Any]  # (institution_id, recipients, title, body)


@dataclass(slots=True)
class ContinuousMonitor:
    service: InternetIntelligenceService
    store: IntelligenceStore
    alert_sink: AlertSink | None = None
    window_days: int = 2
    max_results: int = 30
    lock_seconds: int = RUN_LOCK_SECONDS

    async def run_for(self, institution_id: str) -> dict[str, Any]:
        profile = self.service.profile_for(institution_id)
        if profile is None:
            raise ValueError(f"no intelligence profile for {institution_id}")
        # The run count rotates the topic and alias queries, so the ones one
        # pass cannot fit run on the next.
        rotation = self.store.run_count(institution_id)
        run_id = self.store.try_start_run(institution_id, lock_seconds=self.lock_seconds)
        if run_id is None:
            return {"run_id": None, "institution_id": institution_id, "skipped": "another monitoring run is in progress for this institution", "new": 0, "changed": 0, "duplicate": 0, "excluded": {}, "alerts": 0, "findings": 0}
        try:
            report, kept = await self.service.collect(
                institution_id, requested_by="monitor", question=None, window_days=self.window_days, topics=list(DEFAULT_TOPICS), max_results=self.max_results, persist=True,
                analyse=False, save_report=False, rotation=rotation, run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - the run must record its failure
            self.store.finish_run(institution_id, run_id, status="failed", new_count=0, changed_count=0, duplicate_count=0, excluded_count=0, error=str(exc)[:300])
            raise
        counts = {"new": 0, "changed": 0, "duplicate": 0}
        alerts: list[tuple[str, str, str]] = []
        # Every kept item is compared, not only the top findings of the report:
        # the store's own verdict on each upsert says whether it is new.
        for record in kept:
            event_type = record.get("change") or "new"
            counts[event_type] += 1
            if event_type == "duplicate":
                continue
            summary = f"{record['source_label']}: {record['title']}"
            event_id = self.store.add_event(institution_id, run_id=run_id, document_id=record.get("document_id", ""), event_type=event_type, importance_level=record["importance"], source_type=record["source_type"], title=record["title"], url=record["url"], summary=summary)
            if record["importance"] == "high":
                alerts.append((event_id, record["title"], record["url"]))
        stop_reason = "candidate_limit" if any(warning["code"] == "candidate_limit_reached" for warning in report["warnings"]) else "completed"
        self.store.finish_run(
            institution_id, run_id, status="succeeded", new_count=counts["new"], changed_count=counts["changed"], duplicate_count=counts["duplicate"], excluded_count=sum(report["excluded"].values()),
            queries=report["queries"], stop_reason=stop_reason,
        )
        alerted: list[str] = []
        if alerts and self.alert_sink is not None and profile.alert_recipients:
            body = "\n".join(f"- {title} ({url})" for _, title, url in alerts)
            self.alert_sink(institution_id, profile.alert_recipients, f"{len(alerts)} important public mention(s) of {profile.name}", body)
            alerted = [event_id for event_id, _, _ in alerts]
            self.store.mark_alerted(institution_id, alerted)
        return {"run_id": run_id, "institution_id": institution_id, **counts, "excluded": report["excluded"], "alerts": len(alerted), "findings": len(kept), "queries": report["queries"]}

    async def run_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for institution_id in self.store.monitored_institutions():
            try:
                results.append(await self.run_for(institution_id))
            except Exception as exc:  # noqa: BLE001
                results.append({"institution_id": institution_id, "error": str(exc)[:300]})
        return results


__all__ = ["ContinuousMonitor"]
