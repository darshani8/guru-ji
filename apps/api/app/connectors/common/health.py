"""Safe health probes for local connectors."""

from datetime import datetime, timezone

from ...domain.source_health import Freshness, SourceHealth, SourceHealthStatus


def demo_health(source_id: str, institution_id: str, display_name: str, connector_type: str) -> SourceHealth:
    now = datetime.now(timezone.utc)
    return SourceHealth(
        source_id=source_id,
        institution_id=institution_id,
        display_name=display_name,
        connector_type=connector_type,
        status=SourceHealthStatus.HEALTHY,
        checked_at=now,
        last_success_at=now,
        latency_ms=1,
        freshness=Freshness.CURRENT,
        detail="deterministic in-memory demo connector; no institutional system was contacted",
    )


__all__ = ["demo_health"]
