"""Seed an institution's internet map from a sweep file and record the baseline.

    PYTHONPATH=apps/api python scripts/import_intel_seed.py --institution bgscet --groups BGSCET
    PYTHONPATH=apps/api python scripts/import_intel_seed.py --institution bgscet --all-groups --approved-by "Math IT office, 2026-09-30"

Without --file the bundled 22 September 2026 sweep is used. Importing every
group maps institutions other than your own, so it needs --approved-by.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config.settings import AppSettings
from app.internet_intelligence.map.metrics import record_baseline
from app.internet_intelligence.map.seed import DEFAULT_LOOKALIKES, DEFAULT_SWEEP, import_seed, parse_lookalikes, parse_sweep
from app.internet_intelligence.map.store import MapStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--institution", required=True)
    parser.add_argument("--file", type=Path, default=DEFAULT_SWEEP)
    parser.add_argument("--lookalikes", type=Path, default=DEFAULT_LOOKALIKES)
    parser.add_argument("--groups", nargs="*", default=None, help="sweep groups to import (default: none unless --all-groups)")
    parser.add_argument("--all-groups", action="store_true")
    parser.add_argument("--approved-by", default=None)
    parser.add_argument("--holdout-percent", type=int, default=20)
    args = parser.parse_args()
    if args.all_groups and not args.approved_by:
        parser.error("--all-groups needs --approved-by")
    if not args.all_groups and not args.groups:
        parser.error("name --groups, or use --all-groups with --approved-by")
    settings = AppSettings.from_env()
    store = MapStore(settings.resolved_institution_database_url(), suppression_key=(settings.intelligence_suppression_key or "guru-ji-development-only").encode("utf-8"))
    try:
        rows = parse_sweep(args.file.read_text(encoding="utf-8"))
        lookalikes = parse_lookalikes(args.lookalikes.read_text(encoding="utf-8")) if args.lookalikes else []
        summary = import_seed(store, args.institution, rows, lookalikes=lookalikes, groups=None if args.all_groups else args.groups, holdout_percent=args.holdout_percent, source=args.file.stem)
        baseline = record_baseline(store, args.institution)
    finally:
        store.close()
    print(json.dumps({"summary": summary.as_dict(), "approved_by": args.approved_by, "baseline": baseline}, indent=2, default=str))


if __name__ == "__main__":
    main()
