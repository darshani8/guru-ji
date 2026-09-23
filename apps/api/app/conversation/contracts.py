"""Typed contracts of one conversational turn."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..agents.contracts import AgentResponse
from ..domain.principals import InstitutionScope, Principal
from ..orchestration.answer_synthesizer import AssistantAnswer

LANGUAGES: tuple[str, ...] = ("en-IN", "hi-IN", "kn-IN")
MAX_HISTORY_TURNS = 12
MAX_HISTORY_TURN_CHARS = 1_000
MAX_HISTORY_CHARS = 6_000
MAX_TURN_CHARS = 4_000

Route = Literal["small_talk", "web", "task", "conversation"]


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    role: Literal["user", "assistant"]
    text: str


def bound_history(turns: Iterable[HistoryTurn | Mapping[str, Any]] | None) -> tuple[HistoryTurn, ...]:
    """The most recent turns that fit the limits, oldest first.

    History comes from the client, which is the only place a conversation is
    kept: the server stores no transcript. It is the person's own context, so
    it can steer wording but never grants anything; every tool call is still
    authorised against the verified principal.
    """

    items: list[HistoryTurn] = []
    for raw in turns or ():
        role = raw.role if isinstance(raw, HistoryTurn) else raw.get("role")
        text = raw.text if isinstance(raw, HistoryTurn) else raw.get("text")
        if role not in {"user", "assistant"} or not isinstance(text, str):
            continue
        text = " ".join(text.split())[:MAX_HISTORY_TURN_CHARS]
        if text:
            items.append(HistoryTurn(role, text))  # type: ignore[arg-type]
    kept: list[HistoryTurn] = []
    total = 0
    for turn in reversed(items[-MAX_HISTORY_TURNS:]):
        if total + len(turn.text) > MAX_HISTORY_CHARS:
            break
        kept.append(turn)
        total += len(turn.text)
    return tuple(reversed(kept))


@dataclass(frozen=True, slots=True)
class Turn:
    request_id: str
    principal: Principal
    scope: InstitutionScope
    text: str
    channel: Literal["text", "voice"] = "text"
    mode: Literal["agent", "assistant"] | None = None
    language_hint: str | None = None
    history: tuple[HistoryTurn, ...] = ()
    conversation_id: str | None = None
    approval_id: str | None = None
    run_in_background: bool = False

    def __post_init__(self) -> None:
        text = " ".join(self.text.split())
        if not text:
            raise ValueError("the message must not be blank")
        if len(text) > MAX_TURN_CHARS:
            raise ValueError(f"the message exceeds {MAX_TURN_CHARS} characters")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "history", bound_history(self.history))
        if self.language_hint not in (None, *LANGUAGES):
            object.__setattr__(self, "language_hint", None)


@dataclass(slots=True)
class Reply:
    route: Route
    response: AgentResponse | AssistantAnswer
    language: str
    speech_text: str
    # A turn that may have changed something (an agent command) is never
    # cancelled half-way; a conversational or web turn has no side effects.
    side_effects: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.response.status

    def as_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        if isinstance(self.response, AgentResponse):
            payload = self.response.as_dict(include_data=include_data)
            if self.response.status in {"needs_input", "approval_required"}:
                payload["refusal_reason"] = None
        else:
            payload = self.response.as_dict()
        payload["route"] = self.route
        payload["language"] = self.language
        payload["speech_text"] = self.speech_text
        return payload


__all__ = [
    "LANGUAGES", "MAX_HISTORY_CHARS", "MAX_HISTORY_TURNS", "MAX_HISTORY_TURN_CHARS", "MAX_TURN_CHARS",
    "HistoryTurn", "Reply", "Route", "Turn", "bound_history",
]
