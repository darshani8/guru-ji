"""Continuous monitoring: scheduled searches, change detection, alerts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .query_generator import DEFAULT_TOPICS
from .service import InternetIntelligenceService
from .store import IntelligenceStore

AlertSink = Callable[[str, Sequence[str], str, str], Any]  # (institution_id, recipients, title, body)


@dataclass(slots=True)
class ContinuousMonitor:
    service: InternetIntelligenceService
    store: IntelligenceStore
    alert_sink: AlertSink | None = None
    window_days: int = 2
    max_results: int = 30

    async def run_for(self, institution_id: str) -> dict[str, Any]:
        profile = self.service.profile_for(institution_id)
        if profile is None:
            raise ValueError(f"no intelligence profile for {institution_id}")
        seen_before = {item["canonical_url"]: item for item in self.store.list_documents(institution_id, status=None, limit=1000)}
        run_id = self.store.start_run(institution_id, [])
        try:
            report = await self.service._investigate(institution_id, requested_by="monitor", question=None, window_days=self.window_days, topics=list(DEFAULT_TOPICS), max_results=self.max_results, persist=True)
        except Exception as exc:  # noqa: BLE001 - the run must record its failure
            self.store.finish_run(institution_id, run_id, status="failed", new_count=0, changed_count=0, duplicate_count=0, excluded_count=0, error=str(exc)[:300])
            raise
        counts = {"new": 0, "changed": 0, "duplicate": 0}
        alerts: list[tuple[str, str, str]] = []
        for finding in report["findings"]:
            document = self.store.get_document(institution_id, finding["document_id"]) if finding.get("document_id") else None
            canonical = document["canonical_url"] if document else None
            previous = seen_before.get(canonical) if canonical else None
            if previous is None:
                event_type = "new"
            elif previous.get("content_sha256") != (document or {}).get("content_sha256"):
                event_type = "changed"
            else:
                event_type = "duplicate"
            counts[event_type] += 1
            if event_type == "duplicate":
                continue
            summary = f"{finding['source_label']}: {finding['title']}"
            event_id = self.store.add_event(institution_id, run_id=run_id, document_id=finding.get("document_id", ""), event_type=event_type, importance_level=finding["importance"], source_type=finding["source_type"], title=finding["title"], url=finding["url"], summary=summary)
            if finding["importance"] == "high":
                alerts.append((event_id, finding["title"], finding["url"]))
        self.store.finish_run(institution_id, run_id, status="succeeded", new_count=counts["new"], changed_count=counts["changed"], duplicate_count=counts["duplicate"], excluded_count=sum(report["excluded"].values()))
        alerted: list[str] = []
        if alerts and self.alert_sink is not None and profile.alert_recipients:
            body = "\n".join(f"- {title} ({url})" for _, title, url in alerts)
            self.alert_sink(institution_id, profile.alert_recipients, f"{len(alerts)} important public mention(s) of {profile.name}", body)
            alerted = [event_id for event_id, _, _ in alerts]
            self.store.mark_alerted(institution_id, alerted)
        return {"run_id": run_id, "institution_id": institution_id, **counts, "excluded": report["excluded"], "alerts": len(alerted), "findings": len(report["findings"])}

    async def run_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for institution_id in self.store.monitored_institutions():
            try:
                results.append(await self.run_for(institution_id))
            except Exception as exc:  # noqa: BLE001
                results.append({"institution_id": institution_id, "error": str(exc)[:300]})
        return results


__all__ = ["ContinuousMonitor"]
