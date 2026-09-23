"""Closed inbound voice WebSocket event contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..conversation.contracts import MAX_HISTORY_TURN_CHARS, MAX_HISTORY_TURNS, MAX_TURN_CHARS

# What a client may opt into when it authenticates. Without them the socket
# behaves as before: one ``answer`` event per utterance.
VoiceFeature = Literal["thinking", "speech", "interrupt"]
VoiceLanguage = Literal["en-IN", "hi-IN", "kn-IN"]


class VoiceMessageBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthenticateMessage(VoiceMessageBase):
    type: Literal["auth"] = "auth"
    ticket: str = Field(min_length=1, max_length=512)
    features: list[VoiceFeature] = Field(default_factory=list, max_length=3)


class HistoryItem(VoiceMessageBase):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_HISTORY_TURN_CHARS * 4)


class UtteranceMessage(VoiceMessageBase):
    type: Literal["utterance"] = "utterance"
    text: str = Field(min_length=1, max_length=MAX_TURN_CHARS)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    # "assistant" keeps the read-only connector path; "agent" routes the final
    # transcript through the master agent so voice can perform work.
    mode: Literal["assistant", "agent"] | None = None
    # The recognition language the person chose; the reply follows the
    # language actually spoken.
    language: VoiceLanguage | None = None
    # The conversation so far, kept by the client (the server stores none).
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS * 2)


class InterruptMessage(VoiceMessageBase):
    """The person started talking over the reply: stop it."""

    type: Literal["interrupt"] = "interrupt"
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


class PingMessage(VoiceMessageBase):
    type: Literal["ping"] = "ping"


class CloseMessage(VoiceMessageBase):
    type: Literal["close"] = "close"


VoiceInboundMessage = Annotated[
    AuthenticateMessage | UtteranceMessage | InterruptMessage | PingMessage | CloseMessage,
    Field(discriminator="type"),
]

VOICE_MESSAGE_ADAPTER: TypeAdapter[VoiceInboundMessage] = TypeAdapter(VoiceInboundMessage)


__all__ = [
    "AuthenticateMessage",
    "CloseMessage",
    "HistoryItem",
    "InterruptMessage",
    "PingMessage",
    "UtteranceMessage",
    "VOICE_MESSAGE_ADAPTER",
    "VoiceFeature",
    "VoiceInboundMessage",
    "VoiceLanguage",
]
