"""How the map learns where to look next, and how much it has probably found.

* The gap grid: every mapped entity against the platforms institutions
  use. A cell is covered when the entity has an account there graded B or
  better; the searches for uncovered cells are claimed first and come
  round every few days until something is found.
* Yield: every source keeps a running average of what it finds, so when
  more work is due than a tick can do, productive sources go first (the
  rest still run in due order, so nothing starves).
* Coverage: a capture-recapture estimate of how many accounts exist, from
  two ways of finding them that are roughly independent: the institution's
  own pages (and hubs they link) versus indexes and directories. It is an
  estimate with a stated assumption, not a count.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .connectors.search import PLATFORM_DOMAINS
from .store import GRADE_RANK, MapStore, grade_at_least

# The platforms shown in the grid, and the search domain for each.
GRID_PLATFORMS: dict[str, str] = {
    "instagram": "instagram.com", "facebook": "facebook.com", "youtube": "youtube.com", "linkedin": "linkedin.com", "x": "x.com", "threads": "threads.net",
    "reddit": "reddit.com", "telegram": "t.me", "github": "github.com", "linktree": "linktr.ee",
}
GAP_INTERVAL_SECONDS = 3 * 86400
_SITE_KINDS = frozenset({"official_link", "hub_link", "owner_claim", "subdomain"})
_INDEX_KINDS = frozenset({"search_snippet", "community_record", "directory_record", "api_identity"})


def gap_grid(store: MapStore, institution_id: str, *, max_entities: int = 40, platforms: Sequence[str] = tuple(GRID_PLATFORMS)) -> dict[str, Any]:
    """Entity x platform: the best grade held there, and whether the cell is covered (B or better)."""

    entities = [entity for entity in store.list_entities(institution_id) if entity["kind"] != "lookalike"][:max_entities]
    best: dict[tuple[str, str], str] = {}
    for asset in store.iter_assets(institution_id, kind="account"):
        if asset["entity_id"] and asset["platform"] in platforms and asset["status"] not in {"dead", "disputed"}:
            key = (asset["entity_id"], asset["platform"])
            if GRADE_RANK.get(asset["grade"], 1) > GRADE_RANK.get(best.get(key, "unrated"), 1):
                best[key] = asset["grade"]
    rows = []
    gaps: list[tuple[str, str]] = []
    for entity in entities:
        cells = {}
        for platform in platforms:
            grade = best.get((entity["entity_id"], platform))
            covered = grade is not None and grade_at_least(grade, "B")
            cells[platform] = {"grade": grade, "covered": covered}
            if not covered:
                gaps.append((entity["entity_id"], platform))
        rows.append({"entity_id": entity["entity_id"], "entity": entity["name"], "kind": entity["kind"], "cells": cells, "covered": sum(cell["covered"] for cell in cells.values())})
    total = len(entities) * len(platforms)
    return {"platforms": list(platforms), "rows": rows, "gaps": gaps, "covered": total - len(gaps), "cells": total}


def prioritise_gaps(store: MapStore, institution_id: str, *, now: datetime) -> int:
    """Flag the search sources for uncovered cells so they are claimed first and come round sooner."""

    grid = gap_grid(store, institution_id)
    targets = [f"{entity_id}|{GRID_PLATFORMS[platform]}" for entity_id, platform in grid["gaps"] if GRID_PLATFORMS.get(platform) in PLATFORM_DOMAINS]
    return store.mark_gap_sources(institution_id, "search", targets, now=now.isoformat(), max_interval=GAP_INTERVAL_SECONDS)


def coverage_estimate(store: MapStore, institution_id: str) -> dict[str, Any]:
    """Chapman's capture-recapture estimate of how many accounts exist, per platform and overall.

    Capture one: accounts found through the institution's own pages (and
    the hubs they link). Capture two: accounts found through indexes,
    directories and platform APIs. With n1 and n2 found by each and m by
    both, N = (n1 + 1)(n2 + 1) / (m + 1) - 1. If the two tend to find the
    same prominent accounts, the estimate runs low; treat it as a floor.
    """

    accounts = [asset for asset in store.iter_assets(institution_id, kind="account") if asset["grade"] != "D"]
    evidence = store.evidence_for(institution_id, [asset["asset_id"] for asset in accounts])
    per_platform: dict[str, dict[str, int]] = {}
    for asset in accounts:
        kinds = {row["kind"] for row in evidence.get(asset["asset_id"], []) if row["polarity"] == "supports"}
        bucket = per_platform.setdefault(asset["platform"], {"known": 0, "site": 0, "index": 0, "both": 0})
        bucket["known"] += 1
        site, index = bool(kinds & _SITE_KINDS), bool(kinds & _INDEX_KINDS)
        bucket["site"] += int(site)
        bucket["index"] += int(index)
        bucket["both"] += int(site and index)

    def chapman(bucket: dict[str, int]) -> float | None:
        if not bucket["site"] or not bucket["index"]:
            return None
        return round((bucket["site"] + 1) * (bucket["index"] + 1) / (bucket["both"] + 1) - 1, 1)

    totals = {key: sum(bucket[key] for bucket in per_platform.values()) for key in ("known", "site", "index", "both")}
    overall = chapman(totals)
    return {
        "known": totals["known"], "estimated_total": overall, "coverage": round(totals["known"] / overall, 3) if overall else None,
        "by_platform": {platform: {**bucket, "estimated_total": chapman(bucket)} for platform, bucket in sorted(per_platform.items())},
        "method": "Chapman capture-recapture: the institution's own pages versus indexes and directories",
        "assumption": "The two ways of finding accounts are independent; they are not quite, so read the estimate as a floor.",
    }


__all__ = ["GAP_INTERVAL_SECONDS", "GRID_PLATFORMS", "coverage_estimate", "gap_grid", "prioritise_gaps"]
