"""SQL safety note and identifier validation for future adapters."""

import re

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def validate_identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe SQL identifier")
    return value


def read_only_statement(statement: str) -> str:
    normalized = statement.strip().lower()
    if not normalized.startswith(("select", "with", "show", "explain")):
        raise ValueError("only read-only statements are permitted")
    forbidden = (" insert ", " update ", " delete ", " drop ", " alter ", " truncate ", ";")
    if any(token in f" {normalized} " for token in forbidden):
        raise ValueError("statement contains a prohibited write or multi-statement token")
    return statement.strip()


__all__ = ["read_only_statement", "validate_identifier"]
