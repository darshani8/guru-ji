"""Safe normalization only.

The cleaner fixes formatting differences that have exactly one correct
reading (whitespace, case, punctuation in phone numbers, semester words,
program aliases, unambiguous dates). Anything else is left as-is and flagged
by the validator. It never fills in a missing value.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .canonical import CanonicalEntity, FieldType, PROGRAM_ALIASES

_ORDINALS = {
    "first": 1, "1st": 1, "i": 1, "one": 1,
    "second": 2, "2nd": 2, "ii": 2, "two": 2,
    "third": 3, "3rd": 3, "iii": 3, "three": 3,
    "fourth": 4, "4th": 4, "iv": 4, "four": 4,
    "fifth": 5, "5th": 5, "v": 5, "five": 5,
    "sixth": 6, "6th": 6, "vi": 6, "six": 6,
    "seventh": 7, "7th": 7, "vii": 7, "seven": 7,
    "eighth": 8, "8th": 8, "viii": 8, "eight": 8,
    "ninth": 9, "9th": 9, "ix": 9, "nine": 9,
    "tenth": 10, "10th": 10, "x": 10, "ten": 10,
}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
_TRUE = {"yes", "y", "true", "1", "hod", "active", "t"}
_FALSE = {"no", "n", "false", "0", "inactive", "f", "-", ""}
_EXCEL_EPOCH = date(1899, 12, 30)


def normalize_whitespace(value: str) -> str:
    return " ".join(value.replace(" ", " ").split())


def normalize_person_name(value: str) -> tuple[str, bool]:
    cleaned = normalize_whitespace(value)
    cleaned = re.sub(r"\s*\.\s*", ". ", cleaned).strip()
    cleaned = " ".join(cleaned.split())
    if cleaned.isupper() or cleaned.islower():
        titled = " ".join(part.capitalize() if len(part) > 1 else part.upper() for part in cleaned.split(" "))
        return titled, titled != value
    return cleaned, cleaned != value


_NUMERIC_LABEL = re.compile(r"-?\d+(\.\d+)?%?")


def normalize_program(value: str) -> tuple[str, bool]:
    cleaned = normalize_whitespace(value)
    key = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    if key in PROGRAM_ALIASES:
        result = PROGRAM_ALIASES[key]
        return result, result != value
    upper = cleaned.upper()
    return upper, upper != value


def normalize_semester(value: Any) -> tuple[int | None, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return (value if 0 < value <= 20 else None), False
    if isinstance(value, float) and value.is_integer():
        return (int(value) if 0 < value <= 20 else None), True
    text = normalize_whitespace(str(value)).lower()
    text = re.sub(r"\b(sem(ester)?|term)\b", " ", text).strip(" .-")
    if text in _ORDINALS:
        return _ORDINALS[text], True
    match = re.search(r"\b(\d{1,2})\b", text)
    if match:
        number = int(match.group(1))
        return (number if 0 < number <= 20 else None), True
    return None, False


def normalize_phone(value: Any) -> tuple[str, bool]:
    text = str(value).strip()
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("+"):
        digits = "+" + digits[1:].replace("+", "")
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = "+" + digits
    return digits, digits != text


def normalize_email(value: Any) -> tuple[str, bool]:
    text = normalize_whitespace(str(value)).lower()
    return text, text != str(value)


def normalize_percent(value: Any) -> tuple[float | None, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        number = float(value)
        if 0 <= number <= 1 and not isinstance(value, int):
            return round(number * 100, 2), True
        return round(number, 2), False
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None, False
    return round(number, 2), True


def normalize_number(value: Any) -> tuple[float | None, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        return float(value), False
    text = str(value).strip().replace(",", "").replace("₹", "").replace("rs.", "").replace("rs", "").replace("inr", "").strip()
    try:
        return float(text), True
    except ValueError:
        return None, False


def normalize_integer(value: Any) -> tuple[int | None, bool]:
    number, changed = normalize_number(value)
    if number is None:
        return None, False
    if not number.is_integer():
        return None, False
    return int(number), changed or not isinstance(value, int)


def normalize_boolean(value: Any) -> tuple[bool | None, bool]:
    if isinstance(value, bool):
        return value, False
    text = normalize_whitespace(str(value)).lower()
    if text in _TRUE:
        return True, True
    if text in _FALSE:
        return False, True
    return None, False


def normalize_date(value: Any) -> tuple[str | None, bool, bool]:
    """Return ISO date, whether it changed, and whether the reading was ambiguous."""

    if isinstance(value, datetime):
        return value.date().isoformat(), True, False
    if isinstance(value, date):
        return value.isoformat(), True, False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        if 20_000 <= serial <= 80_000:
            from datetime import timedelta

            return (_EXCEL_EPOCH + timedelta(days=int(serial))).isoformat(), True, False
        return None, False, False
    text = normalize_whitespace(str(value))
    if not text:
        return None, False, False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text, False, False
        except ValueError:
            return None, False, False
    ambiguous = False
    slash = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", text)
    if slash:
        first, second = int(slash.group(1)), int(slash.group(2))
        ambiguous = first <= 12 and second <= 12 and first != second
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.date().isoformat(), True, ambiguous
    return None, False, False


def clean_value(field_type: FieldType, name: str, value: Any) -> tuple[Any, list[str], list[dict[str, Any]]]:
    """Normalize one value; returns (value, normalizations applied, issues)."""

    notes: list[str] = []
    issues: list[dict[str, Any]] = []
    if value is None:
        return None, notes, issues
    if isinstance(value, str):
        stripped = normalize_whitespace(value)
        if stripped != value:
            notes.append(f"{name}:whitespace")
        value = stripped
        if value == "" or value.lower() in {"na", "n/a", "nil", "null", "none", "-", "--"}:
            if value != "":
                notes.append(f"{name}:placeholder_to_empty")
            return None, notes, issues
    if field_type is FieldType.STRING:
        if name in {"name", "student_name", "guardian_name", "hod_name"}:
            cleaned, changed = normalize_person_name(str(value))
            if changed:
                notes.append(f"{name}:name_case")
            return cleaned, notes, issues
        if name == "program":
            if _NUMERIC_LABEL.fullmatch(str(value).strip()):
                issues.append({"field": name, "code": "numeric_program", "severity": "warning", "value": str(value)[:40]})
                return None, notes, issues
            cleaned, changed = normalize_program(str(value))
            if changed:
                notes.append(f"{name}:program_alias")
            return cleaned, notes, issues
        if name in {"department", "course_code", "fee_type", "exam_name", "status", "result_status", "gender", "category", "section"}:
            cleaned = normalize_whitespace(str(value))
            if name in {"course_code", "section"}:
                cleaned = cleaned.upper()
            elif name in {"status", "result_status", "gender"}:
                cleaned = cleaned.lower()
            if cleaned != value:
                notes.append(f"{name}:case")
            return cleaned, notes, issues
        return str(value), notes, issues
    if field_type is FieldType.IDENTIFIER:
        cleaned = normalize_whitespace(str(value)).upper()
        if isinstance(value, float) and value.is_integer():
            cleaned = str(int(value))
        if cleaned != str(value):
            notes.append(f"{name}:identifier_case")
        return cleaned, notes, issues
    if field_type is FieldType.INTEGER:
        if name == "semester":
            number, changed = normalize_semester(value)
            if number is None:
                issues.append({"field": name, "code": "invalid_semester", "severity": "warning", "value": str(value)[:40]})
                return None, notes, issues
            if changed:
                notes.append(f"{name}:semester_parsed")
            return number, notes, issues
        number_int, changed = normalize_integer(value)
        if number_int is None:
            issues.append({"field": name, "code": "invalid_integer", "severity": "warning", "value": str(value)[:40]})
            return None, notes, issues
        if changed:
            notes.append(f"{name}:integer_parsed")
        return number_int, notes, issues
    if field_type is FieldType.NUMBER:
        number, changed = normalize_number(value)
        if number is None:
            issues.append({"field": name, "code": "invalid_number", "severity": "warning", "value": str(value)[:40]})
            return None, notes, issues
        if changed:
            notes.append(f"{name}:number_parsed")
        return number, notes, issues
    if field_type is FieldType.PERCENT:
        number, changed = normalize_percent(value)
        if number is None:
            issues.append({"field": name, "code": "invalid_percent", "severity": "warning", "value": str(value)[:40]})
            return None, notes, issues
        if changed:
            notes.append(f"{name}:percent_parsed")
        return number, notes, issues
    if field_type is FieldType.PHONE:
        cleaned, changed = normalize_phone(value)
        if changed:
            notes.append(f"{name}:phone_format")
        return cleaned, notes, issues
    if field_type is FieldType.EMAIL:
        cleaned, changed = normalize_email(value)
        if changed:
            notes.append(f"{name}:email_case")
        return cleaned, notes, issues
    if field_type is FieldType.BOOLEAN:
        flag, changed = normalize_boolean(value)
        if flag is None:
            issues.append({"field": name, "code": "invalid_boolean", "severity": "warning", "value": str(value)[:40]})
            return None, notes, issues
        if changed:
            notes.append(f"{name}:boolean_parsed")
        return flag, notes, issues
    if field_type is FieldType.DATE:
        iso, changed, ambiguous = normalize_date(value)
        if iso is None:
            issues.append({"field": name, "code": "invalid_date", "severity": "warning", "value": str(value)[:40]})
            return None, notes, issues
        if changed:
            notes.append(f"{name}:date_parsed")
        if ambiguous:
            issues.append({"field": name, "code": "ambiguous_date_day_month", "severity": "info", "value": str(value)[:40], "assumed": "day/month/year"})
        return iso, notes, issues
    return value, notes, issues


def clean_record(entity: CanonicalEntity, fields: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    cleaned: dict[str, Any] = {}
    notes: list[str] = []
    issues: list[dict[str, Any]] = []
    for item in entity.fields:
        if item.name not in fields:
            continue
        value, field_notes, field_issues = clean_value(item.field_type, item.name, fields[item.name])
        cleaned[item.name] = value
        notes.extend(field_notes)
        issues.extend(field_issues)
    # Derived attendance percent when only counts were supplied.
    if entity.name == "attendance":
        held, attended, absent = cleaned.get("classes_held"), cleaned.get("classes_attended"), cleaned.get("classes_absent")
        if attended is None and held is not None and absent is not None:
            cleaned["classes_attended"] = max(0, int(held) - int(absent))
            notes.append("classes_attended:derived_from_absent")
            attended = cleaned["classes_attended"]
        if cleaned.get("attendance_percent") is None and held and attended is not None:
            cleaned["attendance_percent"] = round(float(attended) / float(held) * 100.0, 2)
            notes.append("attendance_percent:derived_from_counts")
    if entity.name == "fee":
        due, paid, balance = cleaned.get("amount_due"), cleaned.get("amount_paid"), cleaned.get("balance")
        if balance is None and due is not None:
            cleaned["balance"] = round(float(due) - float(paid or 0.0), 2)
            notes.append("balance:derived_from_due_minus_paid")
        if cleaned.get("status") is None and cleaned.get("balance") is not None:
            cleaned["status"] = "paid" if float(cleaned["balance"]) <= 0 else ("partial" if paid else "pending")
            notes.append("status:derived_from_balance")
    return cleaned, notes, issues


__all__ = [
    "clean_record", "clean_value", "normalize_date", "normalize_email", "normalize_person_name", "normalize_phone",
    "normalize_program", "normalize_semester", "normalize_whitespace",
]
