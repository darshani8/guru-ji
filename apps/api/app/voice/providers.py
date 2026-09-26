"""Provider-neutral voice event normalization.

LiveKit and Pipecat integrations may emit different event names. Agent Saffron
normalizes only lifecycle/transcript/answer metadata; raw audio stays in the
provider transport and is never stored by this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .realtime_events import RealtimeEvent


_PROVIDER_EVENT_MAP = {
    "livekit.session_started": RealtimeEvent.SESSION_CREATED,
    "livekit.room_connected": RealtimeEvent.READY,
    "livekit.transcription.partial": RealtimeEvent.TRANSCRIPT_PARTIAL,
    "livekit.transcription.final": RealtimeEvent.TRANSCRIPT_FINAL,
    "livekit.room_disconnected": RealtimeEvent.SESSION_CLOSED,
    "pipecat.session_started": RealtimeEvent.SESSION_CREATED,
    "pipecat.transcript.partial": RealtimeEvent.TRANSCRIPT_PARTIAL,
    "pipecat.transcript.final": RealtimeEvent.TRANSCRIPT_FINAL,
    "pipecat.tts.started": RealtimeEvent.ANSWER,
    "pipecat.session_closed": RealtimeEvent.SESSION_CLOSED,
}


@dataclass(frozen=True, slots=True)
class NormalizedVoiceEvent:
    event: RealtimeEvent
    session_id: str
    text: str | None = None
    metadata: tuple[tuple[str, str | int | bool | None], ...] = ()


class VoiceEventNormalizer:
    def normalize(self, provider: str, event_name: str, payload: dict[str, Any]) -> NormalizedVoiceEvent:
        key = f"{provider.strip().lower()}.{event_name.strip().lower()}"
        event = _PROVIDER_EVENT_MAP.get(key)
        if event is None:
            raise ValueError("voice provider event is not allowlisted")
        session_id = payload.get("session_id") or payload.get("room_name") or payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("voice provider event must include a session identifier")
        text_value = payload.get("text") or payload.get("transcript")
        text = text_value.strip()[:12_000] if isinstance(text_value, str) else None
        metadata = tuple(sorted(
            (str(key), value)
            for key, value in payload.items()
            if key in {"language", "speaker", "is_final", "provider_request_id"}
            and isinstance(value, (str, int, bool))
        ))
        return NormalizedVoiceEvent(event=event, session_id=session_id.strip(), text=text, metadata=metadata)


__all__ = ["NormalizedVoiceEvent", "VoiceEventNormalizer"]
