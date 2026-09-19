"""Stable response objects for cited and refusal-aware answers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResponseStatus(StrEnum):
    """Outcome categories that clients can render explicitly."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    REFUSED = "refused"


def _require_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


@dataclass(frozen=True, slots=True)
class Citation:
    """A source reference without copying an institution's underlying records."""

    source_id: str
    title: str
    locator: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "title", "locator"):
            object.__setattr__(
                self,
                field_name,
                _require_text(field_name, getattr(self, field_name)),
            )


@dataclass(frozen=True, slots=True)
class AnswerResponse:
    """A bounded answer contract that preserves citations and refusal reasons."""

    request_id: str
    status: ResponseStatus
    answer: str
    citations: tuple[Citation, ...] = ()
    refusal_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text("request_id", self.request_id))

        status = self.status
        if isinstance(status, str):
            status = ResponseStatus(status)
        object.__setattr__(self, "status", status)

        answer = self.answer.strip()
        if status is not ResponseStatus.REFUSED and not answer:
            raise ValueError("answer must not be blank unless the response is refused")
        object.__setattr__(self, "answer", answer)

        citations = tuple(self.citations)
        if len(citations) > 50:
            raise ValueError("a response cannot contain more than 50 citations")
        object.__setattr__(self, "citations", citations)

        if status is ResponseStatus.REFUSED:
            if self.refusal_reason is None:
                raise ValueError("refusal_reason is required for refused responses")
            object.__setattr__(
                self,
                "refusal_reason",
                _require_text("refusal_reason", self.refusal_reason),
            )
        elif self.refusal_reason is not None:
            raise ValueError("refusal_reason is only valid for refused responses")

    def as_dict(self) -> dict[str, object]:
        """Return a transport-neutral representation for a future API adapter."""

        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "answer": self.answer,
            "citations": [
                {
                    "source_id": citation.source_id,
                    "title": citation.title,
                    "locator": citation.locator,
                }
                for citation in self.citations
            ],
            "refusal_reason": self.refusal_reason,
        }


__all__ = ["AnswerResponse", "Citation", "ResponseStatus"]
