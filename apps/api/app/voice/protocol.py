"""Closed inbound voice WebSocket event contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class VoiceMessageBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthenticateMessage(VoiceMessageBase):
    type: Literal["auth"] = "auth"
    ticket: str = Field(min_length=1, max_length=512)


class UtteranceMessage(VoiceMessageBase):
    type: Literal["utterance"] = "utterance"
    text: str = Field(min_length=1, max_length=12_000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


class PingMessage(VoiceMessageBase):
    type: Literal["ping"] = "ping"


class CloseMessage(VoiceMessageBase):
    type: Literal["close"] = "close"


VoiceInboundMessage = Annotated[
    AuthenticateMessage | UtteranceMessage | PingMessage | CloseMessage,
    Field(discriminator="type"),
]

VOICE_MESSAGE_ADAPTER = TypeAdapter(VoiceInboundMessage)


__all__ = [
    "AuthenticateMessage",
    "CloseMessage",
    "PingMessage",
    "UtteranceMessage",
    "VoiceInboundMessage",
    "VOICE_MESSAGE_ADAPTER",
]
