"""Closed Agent Saffron-owned streaming event contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StreamEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    sequence: int = Field(ge=1)


class MessageStartEvent(StreamEventBase):
    type: Literal["message_start"] = "message_start"


class CitationEvent(StreamEventBase):
    type: Literal["citation"] = "citation"
    citation: dict[str, object]


class WarningEvent(StreamEventBase):
    type: Literal["warning"] = "warning"
    warning: dict[str, object]


class DeltaEvent(StreamEventBase):
    type: Literal["delta"] = "delta"
    text: str = Field(max_length=12_000)


class AnswerEvent(StreamEventBase):
    type: Literal["answer"] = "answer"
    answer: dict[str, object]


class MessageEndEvent(StreamEventBase):
    type: Literal["message_end"] = "message_end"
    status: Literal["complete", "partial", "refused", "failed", "degraded"]


class ErrorEvent(StreamEventBase):
    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)


class DoneEvent(StreamEventBase):
    type: Literal["done"] = "done"


StreamEvent = Annotated[
    MessageStartEvent
    | CitationEvent
    | WarningEvent
    | DeltaEvent
    | AnswerEvent
    | MessageEndEvent
    | ErrorEvent
    | DoneEvent,
    Field(discriminator="type"),
]

STREAM_EVENT_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


class StreamSequenceError(ValueError):
    """Raised when a stream is out of order, duplicated, or emitted after done."""


@dataclass(slots=True)
class StreamSequenceValidator:
    last_sequence: int = 0
    started: bool = False
    ended: bool = False
    done: bool = False

    def accept(self, event: StreamEvent | dict[str, object]) -> StreamEvent:
        validated = validate_event(event)
        if self.done:
            raise StreamSequenceError("events cannot be emitted after done")
        if validated.sequence != self.last_sequence + 1:
            raise StreamSequenceError("stream sequence must be contiguous and monotonic")
        if not self.started and validated.type != "message_start":
            raise StreamSequenceError("message_start must be the first stream event")
        if validated.type == "message_start":
            if self.started:
                raise StreamSequenceError("message_start may only be emitted once")
            self.started = True
        if validated.type == "message_end":
            if self.ended:
                raise StreamSequenceError("message_end may only be emitted once")
            self.ended = True
        if validated.type == "done":
            if not self.ended:
                raise StreamSequenceError("done requires message_end")
            self.done = True
        if self.ended and validated.type in {"citation", "warning", "delta", "answer"}:
            raise StreamSequenceError("content events cannot follow message_end")
        self.last_sequence = validated.sequence
        return validated


def validate_event(event: StreamEvent | dict[str, object]) -> StreamEvent:
    return STREAM_EVENT_ADAPTER.validate_python(event)


def to_sse(event: StreamEvent | dict[str, object]) -> str:
    validated = validate_event(event)
    payload = json.dumps(
        validated.model_dump(mode="json", exclude_none=True),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"event: {validated.type}\ndata: {payload}\n\n"


__all__ = [
    "AnswerEvent",
    "CitationEvent",
    "DeltaEvent",
    "DoneEvent",
    "ErrorEvent",
    "MessageEndEvent",
    "MessageStartEvent",
    "StreamEvent",
    "StreamSequenceError",
    "StreamSequenceValidator",
    "WarningEvent",
    "to_sse",
    "validate_event",
]
