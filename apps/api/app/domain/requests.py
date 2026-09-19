"""Validated request objects shared by API and orchestration layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .principals import InstitutionScope

MAX_PROMPT_CHARS = 12_000
MAX_SOURCE_IDS = 20


class InteractionChannel(StrEnum):
    """Transport channel selected for a request."""

    TEXT = "text"
    VOICE = "voice"


def _require_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A bounded, read-only assistant request with an explicit institution scope."""

    request_id: str
    principal_id: str
    prompt: str
    institution_scope: InstitutionScope
    source_ids: tuple[str, ...] = ()
    conversation_id: str | None = None
    channel: InteractionChannel = InteractionChannel.TEXT

    def __post_init__(self) -> None:
        for field_name in ("request_id", "principal_id"):
            object.__setattr__(self, field_name, _require_text(field_name, getattr(self, field_name)))

        prompt = _require_text("prompt", self.prompt)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
        object.__setattr__(self, "prompt", prompt)

        if self.conversation_id is not None:
            object.__setattr__(
                self,
                "conversation_id",
                _require_text("conversation_id", self.conversation_id),
            )

        channel = self.channel
        if isinstance(channel, str):
            channel = InteractionChannel(channel)
        object.__setattr__(self, "channel", channel)

        source_ids = tuple(_require_text("source_id", source_id) for source_id in self.source_ids)
        if len(source_ids) > MAX_SOURCE_IDS:
            raise ValueError(f"source_ids exceeds {MAX_SOURCE_IDS} entries")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must be unique")
        object.__setattr__(self, "source_ids", source_ids)


__all__ = ["ChatRequest", "InteractionChannel"]
