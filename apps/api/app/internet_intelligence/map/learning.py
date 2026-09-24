"""How the map learns where to look next, and how much it has probably found.

* The gap grid: every mapped entity against the platforms institutions
  use, the institution's own first. A cell is covered when the entity has
  an account or group there graded B or better; the searches for uncovered
  cells are claimed first and come round every few days until something is
  found.
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


def ranked_entities(store: MapStore, institution_id: str, *, own_groups: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Every mapped entity but the look-alikes, the institution's own first.

    Nominated entities (holding a domain the institution configured in its
    profile) come first, then those in the institution's own sweep groups:
    the operator's configured groups (SAFFRON_INTELLIGENCE_SEED_GROUPS) when
    given, else the nominated entities' groups. The rest follow in group and
    name order. Nothing is dropped: a cap over the group-then-name order
    silently left out whole groups (with the bundled sweep, the pilot college
    and the Math sorted past the first forty and were never searched).
    """

    entities = [entity for entity in store.list_entities(institution_id, limit=5000) if entity["kind"] != "lookalike"]
    nominated = {domain["entity_id"] for domain in store.iter_assets(institution_id, kind="domain", relation="official") if domain["entity_id"] and store.has_evidence(institution_id, domain["asset_id"], "configured_domain")}
    groups = {group.strip().lower() for group in own_groups} or {str(entity["group_label"]).strip().lower() for entity in entities if entity["entity_id"] in nominated}
    groups.discard("")
    return sorted(entities, key=lambda entity: 0 if entity["entity_id"] in nominated else 1 if str(entity["group_label"]).strip().lower() in groups else 2)


def gap_grid(store: MapStore, institution_id: str, *, max_entities: int | None = None, own_groups: Sequence[str] = (), platforms: Sequence[str] = tuple(GRID_PLATFORMS)) -> dict[str, Any]:
    """Entity x platform: the best grade held there, and whether the cell is covered (B or better).

    Every entity is in the grid, the institution's own first; ``max_entities``
    shortens it for display, and ``truncated`` and ``entities_total`` say so.
    """

    ranked = ranked_entities(store, institution_id, own_groups=own_groups)
    entities = ranked if max_entities is None else ranked[:max_entities]
    best: dict[tuple[str, str], str] = {}
    # A group (on Facebook or Telegram) is as much a presence on a platform as an account.
    for asset in (asset for kind in ("account", "group") for asset in store.iter_assets(institution_id, kind=kind)):
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
    return {"platforms": list(platforms), "rows": rows, "gaps": gaps, "covered": total - len(gaps), "cells": total, "entities_total": len(ranked), "truncated": len(entities) < len(ranked)}


def prioritise_gaps(store: MapStore, institution_id: str, *, now: datetime) -> int:
    """Flag the search sources for every entity's uncovered cells so they are claimed first and come round sooner."""

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

    Coverage is what either capture found (n1 + n2 - m) over N, never above
    1. Accounts known some other way (an imported claim, a reviewer) are
    outside both captures, so the estimate says nothing about them: they are
    reported as ``other_known``, not counted as found (with the sweep
    imported they outnumbered the estimate and coverage read 2574%).
    """

    accounts = [asset for kind in ("account", "group") for asset in store.iter_assets(institution_id, kind=kind) if asset["grade"] != "D"]
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

    def captured(bucket: dict[str, int]) -> int:
        return bucket["site"] + bucket["index"] - bucket["both"]

    totals = {key: sum(bucket[key] for bucket in per_platform.values()) for key in ("known", "site", "index", "both")}
    overall = chapman(totals)
    return {
        "known": totals["known"], "captured": captured(totals), "other_known": totals["known"] - captured(totals), "estimated_total": overall,
        "coverage": min(1.0, round(captured(totals) / overall, 3)) if overall else None,
        "by_platform": {platform: {**bucket, "captured": captured(bucket), "estimated_total": chapman(bucket)} for platform, bucket in sorted(per_platform.items())},
        "method": "Chapman capture-recapture: the institution's own pages versus indexes and directories",
        "assumption": "The two ways of finding accounts are independent; they are not quite, so read the estimate as a floor.",
    }


__all__ = ["GAP_INTERVAL_SECONDS", "GRID_PLATFORMS", "coverage_estimate", "gap_grid", "prioritise_gaps", "ranked_entities"]
