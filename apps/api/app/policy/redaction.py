"""Recursive redaction for untrusted and sensitive values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

REDACTED = "[REDACTED]"
SENSITIVE_KEY_FRAGMENTS = frozenset({
    "password", "secret", "token", "api_key", "apikey", "credential",
    "connection_string", "private_key", "authorization", "raw_audio",
    "student_identifier", "email", "phone",
})


def _is_sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: REDACTED if _is_sensitive(key) else redact_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return redact_value(value)  # type: ignore[return-value]


__all__ = ["REDACTED", "redact_mapping", "redact_value"]
