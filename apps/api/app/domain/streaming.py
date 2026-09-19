"""Closed Guru Ji-owned streaming event contracts."""

from __future__ import annotations

import json
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

STREAM_EVENT_ADAPTER = TypeAdapter(StreamEvent)


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
    "WarningEvent",
    "to_sse",
    "validate_event",
]
