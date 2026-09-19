"""Stable application error categories."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_REQUIRED = "authentication_required"
    AUTHENTICATION_INVALID = "authentication_invalid"
    AUTHORIZATION_DENIED = "authorization_denied"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_TIMEOUT = "source_timeout"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


@dataclass(frozen=True, slots=True)
class PublicError:
    code: ErrorCode
    message: str
    request_id: str
    retry_after_seconds: int | None = None


class GuruJiError(Exception):
    """Base exception carrying a safe client-facing category."""

    def __init__(self, public_error: PublicError) -> None:
        super().__init__(public_error.message)
        self.public_error = public_error


__all__ = ["ErrorCode", "GuruJiError", "PublicError"]
