"""Regenerate openapi.yaml from the live FastAPI application."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))


def main() -> None:
    from app.main import app

    document = app.openapi()
    (ROOT / "openapi.yaml").write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"OPENAPI_EXPORTED paths={len(document.get('paths', {}))}")


if __name__ == "__main__":
    main()
