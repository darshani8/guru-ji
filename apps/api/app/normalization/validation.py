"""Validation that distinguishes blocking errors from warnings and OCR doubt."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from .canonical import CanonicalEntity, FieldType

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_IDENTIFIER = re.compile(r"^[A-Z0-9][A-Z0-9/_-]{1,29}$")
_CONFUSABLE = str.maketrans({"I": "1", "L": "1", "O": "0", "S": "5", "B": "8", "Z": "2"})
_LETTERS_IN_DIGIT_RUNS = re.compile(r"(?<=\d)[IOLSBZ](?=\d)|(?<=\d)[IOLSBZ]$|^[IOLSBZ](?=\d)")


def _issue(field: str, code: str, severity: str, value: Any = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"field": field, "code": code, "severity": severity}
    if value is not None:
        payload["value"] = str(value)[:60]
    payload.update(extra)
    return payload


def identifier_pattern(values: Sequence[str]) -> str | None:
    """Derive the dominant character-class pattern of a column (e.g. 1MS23MBA001 -> 9AA99AAA999)."""

    shapes = Counter()
    for value in values:
        if not value:
            continue
        shape = "".join("9" if char.isdigit() else ("A" if char.isalpha() else char) for char in str(value))
        shapes[shape] += 1
    if not shapes:
        return None
    shape, count = shapes.most_common(1)[0]
    return shape if count >= max(3, int(sum(shapes.values()) * 0.5)) else None


def ocr_suspicion(value: str, expected_pattern: str | None) -> dict[str, Any] | None:
    """Flag identifiers whose letters sit where the column's dominant pattern has digits."""

    text = str(value)
    if expected_pattern and len(text) == len(expected_pattern):
        mismatches = [
            index for index, (char, expected) in enumerate(zip(text, expected_pattern))
            if expected == "9" and char.isalpha()
        ]
        if mismatches and all(text[index] in "IOLSBZ" for index in mismatches):
            suggestion = "".join(char.translate(_CONFUSABLE) if index in mismatches else char for index, char in enumerate(text))
            return _issue("student_id", "possible_ocr_error", "warning", text, suggested=suggestion)
    elif _LETTERS_IN_DIGIT_RUNS.search(text):
        return _issue("student_id", "possible_ocr_error", "warning", text, suggested=text.translate(_CONFUSABLE))
    return None


def validate_record(entity: CanonicalEntity, fields: dict[str, Any], *, ocr: bool = False, ocr_confidence: float | None = None, id_pattern: str | None = None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for name in entity.required_fields():
        if fields.get(name) in (None, ""):
            if name == "name" and (fields.get("first_name") or fields.get("last_name")):
                continue
            issues.append(_issue(name, "missing_required", "error"))
    for item in entity.fields:
        value = fields.get(item.name)
        if value in (None, ""):
            continue
        if item.field_type is FieldType.IDENTIFIER:
            if not _IDENTIFIER.fullmatch(str(value)):
                issues.append(_issue(item.name, "invalid_identifier", "warning", value))
            if ocr or (id_pattern and item.name in entity.natural_key):
                suspicion = ocr_suspicion(str(value), id_pattern)
                if suspicion:
                    suspicion["field"] = item.name
                    issues.append(suspicion)
        elif item.field_type is FieldType.PHONE:
            digits = re.sub(r"\D", "", str(value))
            if not 7 <= len(digits) <= 15:
                issues.append(_issue(item.name, "invalid_phone", "warning", value))
        elif item.field_type is FieldType.EMAIL:
            if not _EMAIL.fullmatch(str(value)):
                issues.append(_issue(item.name, "invalid_email", "warning", value))
        elif item.field_type is FieldType.PERCENT:
            if not 0 <= float(value) <= 100:
                issues.append(_issue(item.name, "percent_out_of_range", "error", value))
        elif item.field_type is FieldType.NUMBER and item.name in {"amount_due", "amount_paid", "marks_obtained", "max_marks", "credits"}:
            if float(value) < 0:
                issues.append(_issue(item.name, "negative_amount", "error", value))
        elif item.field_type is FieldType.INTEGER and item.name in {"classes_held", "classes_attended", "classes_absent", "intake", "admission_year"}:
            if int(value) < 0:
                issues.append(_issue(item.name, "negative_count", "error", value))
            if item.name == "admission_year" and not 1900 <= int(value) <= 2100:
                issues.append(_issue(item.name, "year_out_of_range", "warning", value))
    if entity.name == "attendance":
        held, attended = fields.get("classes_held"), fields.get("classes_attended")
        if held is not None and attended is not None and int(attended) > int(held):
            issues.append(_issue("classes_attended", "attended_exceeds_held", "error", attended, classes_held=held))
        if fields.get("attendance_percent") is None and (held in (None, 0) or attended is None):
            issues.append(_issue("attendance_percent", "attendance_unknown", "warning"))
    if entity.name == "exam":
        marks, maximum = fields.get("marks_obtained"), fields.get("max_marks")
        if marks is not None and maximum is not None and float(marks) > float(maximum):
            issues.append(_issue("marks_obtained", "marks_exceed_maximum", "error", marks, max_marks=maximum))
    if entity.name == "fee":
        due, paid = fields.get("amount_due"), fields.get("amount_paid")
        if due is not None and paid is not None and float(paid) > float(due) * 1.5 and float(due) > 0:
            issues.append(_issue("amount_paid", "paid_far_exceeds_due", "warning", paid, amount_due=due))
    if ocr:
        if ocr_confidence is not None and ocr_confidence < 0.75:
            issues.append(_issue("*", "low_ocr_confidence", "warning", ocr_confidence))
        else:
            issues.append(_issue("*", "ocr_source_verify", "info"))
    return issues


def summarize_issues(issues_by_row: Sequence[Sequence[dict[str, Any]]]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    rows_with_errors = 0
    for issues in issues_by_row:
        if any(item.get("severity") == "error" for item in issues):
            rows_with_errors += 1
        for item in issues:
            counter[f"{item.get('field')}:{item.get('code')}"] += 1
            severities[str(item.get("severity"))] += 1
    return {"rows_with_errors": rows_with_errors, "by_code": dict(counter.most_common(30)), "by_severity": dict(severities)}


__all__ = ["identifier_pattern", "ocr_suspicion", "summarize_issues", "validate_record"]
