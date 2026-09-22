"""Export the map in the manual sweep's own table format, so the two can be compared line by line."""

from __future__ import annotations

import csv
import io
from typing import Any

from .store import GRADE_RANK, MapStore

COLUMNS = ("group", "entity", "url", "grade", "relation", "status", "last_verified", "note")


def export_rows(store: MapStore, institution_id: str, *, min_grade: str | None = None) -> list[dict[str, Any]]:
    entities = {entity["entity_id"]: entity for entity in store.list_entities(institution_id)}
    rows: list[dict[str, Any]] = []
    for asset in store.list_assets(institution_id, limit=5000):
        if min_grade and GRADE_RANK.get(asset["grade"], 1) < GRADE_RANK[min_grade]:
            continue
        entity = entities.get(asset["entity_id"] or "", {})
        reasons = "; ".join(asset.get("grade_reasons") or [])
        rows.append({
            "group": entity.get("group_label", ""), "entity": entity.get("name", ""), "url": asset["url"], "grade": asset["grade"], "relation": asset["relation"],
            "status": asset["status"], "last_verified": (asset.get("last_verified_at") or "")[:10], "note": " · ".join(part for part in (reasons, asset.get("note") or "") if part)[:500],
        })
    rows.sort(key=lambda row: (row["group"], row["entity"], -GRADE_RANK.get(row["grade"], 1), row["url"]))
    return rows


def export_tsv(store: MapStore, institution_id: str, *, min_grade: str | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in export_rows(store, institution_id, min_grade=min_grade):
        writer.writerow({key: str(value).replace("\t", " ").replace("\n", " ") for key, value in row.items()})
    return buffer.getvalue()


__all__ = ["COLUMNS", "export_rows", "export_tsv"]
