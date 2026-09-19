"""Reject files that must never be committed to the application repository."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production"}


def _files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_DIRECTORIES for part in path.relative_to(ROOT).parts)
    ]


def main() -> None:
    violations: list[str] = []
    for path in _files():
        relative = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_NAMES or path.name.lower().endswith(FORBIDDEN_SUFFIXES):
            violations.append(relative)
        if relative.startswith("data/") and path.name != ".gitkeep":
            violations.append(relative)
    if violations:
        unique = sorted(set(violations))
        raise SystemExit("forbidden repository artifacts: " + ", ".join(unique))
    print("REPOSITORY_HYGIENE_OK")


if __name__ == "__main__":
    main()
