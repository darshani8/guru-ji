"""Repairs for speech-to-text mishearings of program codes and "fees"."""

from __future__ import annotations

import re

# Speech-to-text often splits or mishears program codes and "fees". Only
# forms that cannot mean anything else in a college question are rewritten.
_SPOKEN_FIXES = (
    (re.compile(r"\b(?:b\.?\s+com|bee\s+com|bcom|b\.com)\b", re.IGNORECASE), "BCOM"),
    (re.compile(r"\bbecome\b(?=\s+(?:students?|fees?|degree|program(?:me)?|course|batch|admissions?)\b)", re.IGNORECASE), "BCOM"),
    (re.compile(r"\bb\s*\.?\s*c\s*\.?\s*a\b", re.IGNORECASE), "BCA"),
    (re.compile(r"\bb\s*\.?\s*b\s*\.?\s*a\b", re.IGNORECASE), "BBA"),
    (re.compile(r"\bm\s*\.?\s*b\s*\.?\s*a\b", re.IGNORECASE), "MBA"),
    (re.compile(r"\b(?:face|faces|phase|phases|fis|feez)\b(?=\s+(?:(?:being|are|is|were|was)\s+)?(?:collection|collected|paid|payment|amount|details|summary|report)\b)", re.IGNORECASE), "fees"),
    (re.compile(r"\b(?:fees|face|faces|phase|phases)\s+(being|were|are)\s+connected\b", re.IGNORECASE), r"fees \1 collected"),
    (re.compile(r"\bfinancial era\b", re.IGNORECASE), "financial year"),
    (re.compile(r"\b(collected|collection|total|pending|paid)\s+(?:face|phase|feez)\b", re.IGNORECASE), r"\1 fees"),
)


def normalize_spoken(text: str) -> str:
    for pattern, replacement in _SPOKEN_FIXES:
        text = pattern.sub(replacement, text)
    return text


__all__ = ["normalize_spoken"]
