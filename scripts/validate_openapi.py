"""Validate the checked-in API contract against the live FastAPI route surface."""

from __future__ import annotations

from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
OPENAPI_PATH = ROOT / "openapi.yaml"
REQUIRED_PATHS = {
    "/v1/health/live",
    "/v1/health/ready",
    "/v1/chat",
    "/v1/chat/stream",
    "/v1/voice/sessions",
    "/v1/voice/sessions/{session_id}",
    "/v1/sources",
    "/v1/briefings/daily",
    "/v1/briefings/recent",
    "/v1/research/web",
    "/v1/audit/recent",
    "/v1/ingestion/uploads",
    "/v1/ingestion/jobs/{job_id}",
    "/v1/ingestion/jobs/{job_id}/mapping",
    "/v1/ingestion/jobs/{job_id}/commit",
    "/v1/ingestion/reviews/{review_id}",
    "/v1/data/summary",
    "/v1/data/students",
    "/v1/data/attendance/low",
    "/v1/data/fees/pending",
    "/v1/agent/commands",
    "/v1/agent/tools",
    "/v1/agent/approvals/{approval_id}",
    "/v1/documents",
    "/v1/documents/search",
    "/v1/documents/evaluate",
    "/v1/intelligence/profile",
    "/v1/intelligence/investigate",
    "/v1/intelligence/digest",
    "/v1/reports/{report_id}/download",
    "/v1/notifications",
    "/v1/institutions/{institution_id}",
}
REQUIRED_SCHEMAS = {
    "ScopeBody",
    "ChatBody",
    "VoiceSessionBody",
    "BriefingBody",
    "WebResearchBody",
    "CommandBody",
    "MappingDecisionBody",
    "ReviewDecisionBody",
    "DocumentSearchBody",
    "DocumentEvalBody",
    "ProfileBody",
    "InvestigateBody",
    "InstitutionBody",
}


def main() -> None:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("OpenAPI document must be a mapping")
    if document.get("openapi") != "3.1.0":
        raise SystemExit("OpenAPI version must remain 3.1.0")

    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SystemExit("OpenAPI paths must be a mapping")
    missing_paths = sorted(REQUIRED_PATHS - set(paths))
    if missing_paths:
        raise SystemExit(f"missing required API paths: {', '.join(missing_paths)}")
    non_versioned = sorted(path for path in paths if not path.startswith("/v1/"))
    if non_versioned:
        raise SystemExit(f"unexpected non-versioned API paths: {', '.join(non_versioned)}")

    from app.main import app

    live_paths = set(app.openapi().get("paths", {}))
    if live_paths != set(paths):
        missing_from_contract = sorted(live_paths - set(paths))
        extra_in_contract = sorted(set(paths) - live_paths)
        raise SystemExit(
            f"OpenAPI route drift; missing={missing_from_contract}, extra={extra_in_contract}"
        )

    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        raise SystemExit("OpenAPI components.schemas must be a mapping")
    missing_schemas = sorted(REQUIRED_SCHEMAS - set(schemas))
    if missing_schemas:
        raise SystemExit(f"missing required API schemas: {', '.join(missing_schemas)}")

    serialized = OPENAPI_PATH.read_text(encoding="utf-8").lower()
    if any(
        secret_marker in serialized for secret_marker in ("api_key:", "bearer_token:", "password:")
    ):
        raise SystemExit("OpenAPI contract appears to contain a secret-bearing example")
    print("OPENAPI_VALIDATION_OK")


if __name__ == "__main__":
    main()
