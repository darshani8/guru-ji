"""Seed an institution's internet map from a sweep file and record the baseline.

    PYTHONPATH=apps/api python scripts/import_intel_seed.py --institution bgscet --groups BGSCET
    PYTHONPATH=apps/api python scripts/import_intel_seed.py --institution bgscet --all-groups --approved-by "Math IT office, 2026-09-30"

Without --file the bundled 22 September 2026 sweep is used. The import goes
through the same service and rules as the API: groups outside the
institution's own (GURU_INTELLIGENCE_SEED_GROUPS), every group, or a custom
file need --approved-by. The run is written to the audit log as the operator
who ran it, and the approval is kept with the baseline run.
"""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from uuid import uuid4

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings
from app.domain.audit import AuditEvent, AuditOutcome
from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.internet_intelligence.map.seed import DEFAULT_LOOKALIKES, DEFAULT_SWEEP


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
    if not args.all_groups and not args.groups:
        parser.error("name --groups, or use --all-groups with --approved-by")
    runtime = build_runtime(AppSettings.from_env())
    platform = runtime.platform
    if platform is None or platform.intelligence_map is None:
        raise SystemExit("the internet map is not enabled (GURU_INTELLIGENCE_MAP_ENABLED)")
    operator = Principal(f"cli:{getpass.getuser()}", PrincipalType.SYSTEM, frozenset({Capability.INTELLIGENCE_MANAGE}), (InstitutionScope(args.institution),))
    outcome, metadata = AuditOutcome.SUCCESS, {"institution_id": args.institution, "all_groups": args.all_groups, "approved_by": args.approved_by, "source": args.file.name}
    try:
        result = platform.intelligence_map.seed(
            operator, args.institution, sweep_text=args.file.read_text(encoding="utf-8"), lookalikes_text=args.lookalikes.read_text(encoding="utf-8") if args.lookalikes else "",
            groups=args.groups, all_groups=args.all_groups, approved_by=args.approved_by, holdout_percent=args.holdout_percent, source=args.file.stem,
        )
        metadata.update({"groups": ",".join(result["summary"]["groups"])[:500], "needed_approval": bool(result["needed_approval"]), "assets_created": int(result["summary"]["assets_created"])})
    except ValueError as exc:
        outcome, result = AuditOutcome.FAILED, None
        parser.error(str(exc))
    finally:
        runtime.store.append_audit(AuditEvent(
            event_id=f"audit-{uuid4().hex}", event_type="intelligence.map.seed", request_id=f"cli-{uuid4().hex}", principal_id=operator.principal_id, endpoint="scripts/import_intel_seed.py",
            source_ids=("public_web",), tool_names=("intelligence.map.seed",), outcome=outcome, decision_metadata=tuple(sorted(metadata.items())),
        ))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
