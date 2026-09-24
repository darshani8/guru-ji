"""One live voice conversation over a WebSocket, full duplex.

The socket keeps reading while Agentic Saffron thinks and speaks, so the person can
talk over a reply: an ``interrupt`` (or a new utterance) stops the reply that
is playing. Each turn runs as its own task and every send goes through one
lock. A conversational or web turn is cancelled outright; a turn that reached
the master agent is never cut short (an action is never left half-done): it
finishes and its answer is shown with ``"spoken": false``.

Spoken replies arrive as ``speech`` events, one per sentence group, carrying
Polly audio when it is configured or only the text for the browser's voice,
and end with ``speech_end``. A model reply is streamed: its first sentence is
sent (and spoken) while the rest is still being written, so ``speech`` can
come before ``answer``. If a streamed reply fails after it started speaking,
``cancelled`` with reason ``retracted`` tells the client to stop, and the
fallback reply is spoken instead. When nothing has been said for a moment, a
short "one moment" filler is spoken. Clients that did not opt into these
features get exactly one ``answer`` per utterance, as before.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..conversation.contracts import HistoryTurn, Turn
from ..conversation.language import DetectedLanguage, detect_language
from ..conversation.phrases import phrase
from ..domain.principals import Principal
from .protocol import ClientLogMessage, CloseMessage, InterruptMessage, PingMessage, UtteranceMessage, VOICE_MESSAGE_ADAPTER
from .realtime_events import RealtimeEvent
from .session_manager import VoiceSession
from .speech_text import speech_chunks

logger = logging.getLogger("saffron.voice")

MAX_EVENT_BYTES = 32_000
# At most this many turns in flight on one socket (one being answered, one
# protected agent action finishing, one new): more is a client bug or abuse.
MAX_TURNS_IN_FLIGHT = 3
# The browser reports what its speech recognition did; one log line per connection at most this often.
CLIENT_LOG_INTERVAL_SECONDS = 4.0


@dataclass(slots=True)
class _Utterance:
    """One sentence group waiting to be sent, with its speech being synthesised."""

    text: str
    language: str
    hinglish: bool
    filler: bool
    final: bool
    generation: int
    audio: asyncio.Task[Any] | None


@dataclass(slots=True)
class _TurnState:
    client_message_id: str
    task: asyncio.Task[None] | None = None
    protected: bool = False  # reached the master agent: finish, never cancel
    muted: bool = False  # superseded: finish silently
    # Speech goes out in order through one sender per turn; bumping the
    # generation drops whatever was queued before (a retracted reply).
    speech_queue: asyncio.Queue[_Utterance | None] = field(default_factory=asyncio.Queue)
    sender: asyncio.Task[None] | None = None
    generation: int = 0
    seq: int = 0
    spoken_any: bool = False
    started: float = field(default_factory=monotonic)
    first_speech_ms: int | None = None


@dataclass(slots=True)
class VoiceConnection:
    websocket: WebSocket
    runtime: Any
    session: VoiceSession
    principal: Principal
    features: frozenset[str] = frozenset()
    utterances_per_minute: int = 20
    idle_timeout_seconds: float = 180.0
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _turns: dict[str, _TurnState] = field(default_factory=dict)
    _utterance_times: deque[float] = field(default_factory=deque)
    _client_logged_at: float = -1e9
    _open: bool = True

    # ------------------------------------------------------------ sending
    async def send(self, payload: dict[str, Any]) -> bool:
        if not self._open:
            return False
        async with self._send_lock:
            try:
                await self.websocket.send_json(payload)
                return True
            except (RuntimeError, WebSocketDisconnect, OSError):
                # The peer went away mid-turn; the read loop will see it.
                self._open = False
                return False

    async def error(self, code: str, message: str, client_message_id: str | None = None) -> None:
        payload: dict[str, Any] = {"type": RealtimeEvent.ERROR, "code": code, "message": message}
        if client_message_id:
            payload["client_message_id"] = client_message_id
        await self.send(payload)

    # ------------------------------------------------------------ main loop
    async def run(self) -> str:
        """Serve the conversation; returns the reason it ended."""

        idle_deadline = monotonic() + self.idle_timeout_seconds
        try:
            while True:
                now = monotonic()
                session_left = (self.session.expires_at.timestamp() - _wall_now())
                remaining = min(idle_deadline - now, session_left)
                if remaining <= 0:
                    await self.send({"type": RealtimeEvent.EXPIRED, "reason": "idle" if idle_deadline - now <= 0 else "session_limit"})
                    return "expired"
                try:
                    message = await asyncio.wait_for(self.websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if message.get("type") == "websocket.disconnect":
                    return "disconnected"
                if message.get("bytes") is not None:
                    await self.error("binary_audio_not_supported", "Send final text transcripts only.")
                    return "binary_audio_not_supported"
                raw_text = message.get("text")
                if raw_text is None or len(raw_text.encode("utf-8")) > MAX_EVENT_BYTES:
                    await self.error("message_too_large", "Voice event exceeds the transport limit.")
                    return "message_too_large"
                try:
                    event = VOICE_MESSAGE_ADAPTER.validate_json(raw_text)
                except ValidationError:
                    await self.error("invalid_event", "Voice events must match the supported JSON schema.")
                    continue
                if isinstance(event, PingMessage):
                    await self.send({"type": RealtimeEvent.PONG})
                    continue
                if isinstance(event, CloseMessage):
                    await self.send({"type": RealtimeEvent.SESSION_CLOSED, "session_id": self.session.session_id})
                    return "client_closed"
                if isinstance(event, InterruptMessage):
                    idle_deadline = monotonic() + self.idle_timeout_seconds
                    await self._interrupt(event.client_message_id, reason="interrupted")
                    continue
                if isinstance(event, ClientLogMessage):
                    self._log_client(event)
                    continue
                if not isinstance(event, UtteranceMessage):
                    await self.error("authentication_not_allowed", "Authentication is only valid as the first event.")
                    continue
                idle_deadline = monotonic() + self.idle_timeout_seconds
                if self.runtime.voice.get(self.session.session_id) is None:
                    # Closed from elsewhere (the person opened voice again, or signed out).
                    await self.send({"type": RealtimeEvent.EXPIRED, "reason": "closed"})
                    return "expired"
                if not self._allow_utterance():
                    await self.error("rate_limited", "Voice utterance limit reached for this session.", event.client_message_id)
                    continue
                await self._start_turn(event)
        finally:
            await self._shutdown()

    def _log_client(self, event: ClientLogMessage) -> None:
        """One line per report, at most every few seconds per connection; counts only, no words."""

        now = monotonic()
        if now - self._client_logged_at < CLIENT_LOG_INTERVAL_SECONDS:
            return
        self._client_logged_at = now
        logger.info(
            "voice client session=%s browser=%s starts=%d ends=%d restarts=%d interim=%d finals=%d dropped_echo=%d sent=%d errors=%s mic_peak=%d",
            self.session.session_id[:8], event.browser or "-", event.starts, event.ends, event.restarts, event.interim, event.finals,
            event.dropped_echo, event.sent, ",".join(event.errors) or "-", event.level_peak,
        )

    # ------------------------------------------------------------ turns
    def _allow_utterance(self) -> bool:
        now = monotonic()
        while self._utterance_times and now - self._utterance_times[0] >= 60:
            self._utterance_times.popleft()
        if len(self._utterance_times) >= self.utterances_per_minute:
            return False
        self._utterance_times.append(now)
        return True

    async def _start_turn(self, event: UtteranceMessage) -> None:
        client_message_id = event.client_message_id or f"voice-{uuid4().hex[:12]}"
        # A new turn supersedes whatever is still being answered or spoken.
        await self._interrupt(None, reason="superseded")
        live = [state for state in self._turns.values() if state.task and not state.task.done()]
        if len(live) >= MAX_TURNS_IN_FLIGHT:
            await self.error("busy", "Still finishing earlier requests; try again in a moment.", client_message_id)
            return
        for finished in [key for key, item in self._turns.items() if item.task is not None and item.task.done()]:
            del self._turns[finished]
        state = _TurnState(client_message_id)
        self._turns[client_message_id] = state
        state.task = asyncio.create_task(self._run_turn(state, event), name=f"voice-turn-{client_message_id}")

    async def _interrupt(self, client_message_id: str | None, *, reason: str) -> None:
        """Stop the reply being prepared or spoken (a named one, or every live one)."""

        live = [state for state in self._turns.values() if state.task is not None and not state.task.done() and not state.muted]
        if client_message_id is not None and client_message_id in self._turns:
            live = [state for state in live if state.client_message_id == client_message_id] or live
        for state in live:
            state.muted = True
            if not state.protected and state.task is not None:
                state.task.cancel()
                if self.features & {"interrupt", "thinking"}:
                    await self.send({"type": RealtimeEvent.CANCELLED, "client_message_id": state.client_message_id, "reason": reason})

    async def _run_turn(self, state: _TurnState, event: UtteranceMessage) -> None:
        request_id = f"voice-{uuid4().hex}"
        speaks = "speech" in self.features
        filler: asyncio.Task[None] | None = None
        try:
            try:
                turn = Turn(
                    request_id, self.principal, self.session.institution_scope, event.text, channel="voice",
                    mode=event.mode or self.runtime.settings.voice_agent_mode, language_hint=event.language,
                    history=tuple(HistoryTurn(item.role, item.text) for item in event.history),
                    conversation_id=event.conversation_id.strip() if event.conversation_id else None,
                )
            except ValueError as exc:
                await self.error("invalid_utterance", str(exc), state.client_message_id)
                return
            if "thinking" in self.features:
                await self.send({"type": RealtimeEvent.THINKING, "client_message_id": state.client_message_id})
            filler_after = self.runtime.settings.voice_filler_after_ms
            if speaks and filler_after > 0:
                filler = asyncio.create_task(self._filler(state, filler_after / 1000, detect_language(turn.text, turn.language_hint)))

            async def on_progress(stage: str, text: str, language: DetectedLanguage) -> None:
                if stage in {"agent", "conversation"}:
                    # Protected only while the master agent may be changing records.
                    state.protected = stage == "agent"
                    return
                if text and speaks:
                    # A progress phrase ("let me check the internet") is heard before the work it announces.
                    self._say(state, text, language.code, hinglish=language.hinglish, filler=True, cacheable=True)
                    await state.speech_queue.join()

            async def on_speech(sentence: str, language: DetectedLanguage) -> None:
                self._say(state, sentence, language.code, hinglish=language.hinglish)

            reply = await self.runtime.dialogue.respond(turn, on_progress=on_progress, on_speech=on_speech if speaks else None)
            if filler is not None:
                filler.cancel()
            state.protected = state.protected or reply.side_effects
            if reply.extras.get("retract_speech"):
                # What was already said came from a reply that then failed:
                # stop it and say the fallback reply instead.
                await self._retract(state)
            payload = reply.as_dict(include_data=False)
            payload["spoken"] = not state.muted
            await self.send({"type": RealtimeEvent.ANSWER, "client_message_id": state.client_message_id, "answer": payload})
            if speaks and not state.muted:
                if not reply.extras.get("spoken_live") or reply.extras.get("retract_speech"):
                    chunks = speech_chunks(reply.speech_text, language=reply.language, max_chars=self.runtime.settings.voice_tts_max_chars_per_reply)
                    for index, chunk in enumerate(chunks):
                        self._say(state, chunk, reply.language, hinglish=bool(reply.extras.get("hinglish")), final=index == len(chunks) - 1)
                count = await self._finish_speech(state)
                await self.send({"type": RealtimeEvent.SPEECH_END, "client_message_id": state.client_message_id, "count": count})
            logger.info(
                "voice turn route=%s status=%s language=%s ms=%d first_speech_ms=%s muted=%s", reply.route, reply.status, reply.language,
                int((monotonic() - state.started) * 1000), state.first_speech_ms, state.muted,
            )
            self.runtime.tracer.record("voice.turn", trace_id=request_id, attributes={
                "request_id": request_id, "route": reply.route, "status": reply.status, "language": reply.language, "channel": "voice",
                "latency_ms": state.first_speech_ms if state.first_speech_ms is not None else int((monotonic() - state.started) * 1000),
            })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one failed turn must not end the conversation
            logger.exception("voice turn failed request_id=%s", request_id)
            await self.error("turn_failed", "Sorry, something went wrong answering that. Please try again.", state.client_message_id)
        finally:
            if filler is not None:
                filler.cancel()
            self._stop_speech(state)

    async def _filler(self, state: _TurnState, delay: float, language: DetectedLanguage) -> None:
        """Say "one moment" once when nothing has been said by ``delay`` seconds into the turn."""

        await asyncio.sleep(delay)
        if not state.spoken_any and not state.muted and state.task is not None and not state.task.done():
            self._say(state, phrase("thinking_filler", language), language.code, hinglish=language.hinglish, filler=True, cacheable=True)

    # ------------------------------------------------------------ speech
    def _say(self, state: _TurnState, text: str, language: str, *, hinglish: bool, filler: bool = False, final: bool = False, cacheable: bool = False) -> None:
        """Queue one sentence group; its synthesis starts now, so later ones overlap sending earlier ones."""

        if state.muted or not text.strip():
            return
        state.spoken_any = True
        tts = self.runtime.tts
        polly_language = "hi-IN" if hinglish else language
        audio = asyncio.create_task(tts.synthesize(text, language=polly_language, cacheable=cacheable)) if tts.supports(polly_language) else None
        state.speech_queue.put_nowait(_Utterance(text, language, hinglish, filler, final, state.generation, audio))
        if state.sender is None or state.sender.done():
            state.sender = asyncio.create_task(self._send_speech(state), name=f"voice-speech-{state.client_message_id}")

    async def _send_speech(self, state: _TurnState) -> None:
        while True:
            item = await state.speech_queue.get()
            try:
                if item is None:
                    return
                await self._send_one(state, item)
            finally:
                state.speech_queue.task_done()

    async def _send_one(self, state: _TurnState, item: _Utterance) -> None:
        speech = await item.audio if item.audio is not None else None
        payload = {
            "type": RealtimeEvent.SPEECH, "client_message_id": state.client_message_id, "text": item.text,
            "language": item.language, "hinglish": item.hinglish, "filler": item.filler, "final": item.final,
            "audio_base64": base64.b64encode(speech.audio).decode("ascii") if speech else None,
            "format": speech.format if speech else None, "voice": speech.voice if speech else None,
        }
        # Checked under the send lock, so nothing from a retracted or
        # silenced reply can slip out after the event that stopped it.
        async with self._send_lock:
            if state.muted or item.generation != state.generation or not self._open:
                return
            payload["seq"] = state.seq
            try:
                await self.websocket.send_json(payload)
            except (RuntimeError, WebSocketDisconnect, OSError):
                self._open = False
                return
            state.seq += 1
            if state.first_speech_ms is None and not item.filler:
                state.first_speech_ms = int((monotonic() - state.started) * 1000)

    async def _finish_speech(self, state: _TurnState) -> int:
        """Wait until everything queued for this turn has been sent; returns how many were sent."""

        if state.sender is not None and not state.sender.done():
            state.speech_queue.put_nowait(None)
            await state.sender
        return state.seq

    async def _retract(self, state: _TurnState) -> None:
        async with self._send_lock:
            state.generation += 1
        self._drain(state)
        if self.features & {"interrupt", "thinking"}:
            await self.send({"type": RealtimeEvent.CANCELLED, "client_message_id": state.client_message_id, "reason": "retracted"})

    @staticmethod
    def _drain(state: _TurnState) -> None:
        while not state.speech_queue.empty():
            item = state.speech_queue.get_nowait()
            state.speech_queue.task_done()
            if item is not None and item.audio is not None and not item.audio.done():
                item.audio.cancel()

    def _stop_speech(self, state: _TurnState) -> None:
        self._drain(state)
        if state.sender is not None and not state.sender.done():
            state.sender.cancel()

    async def _shutdown(self) -> None:
        self._open = False
        tasks = [state.task for state in self._turns.values() if state.task is not None and not state.task.done()]
        for state in self._turns.values():
            if state.task is not None and not state.task.done() and not state.protected:
                state.task.cancel()
        if tasks:
            # A protected agent turn is allowed a moment to finish its action.
            await asyncio.wait(tasks, timeout=30)


def _wall_now() -> float:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).timestamp()


__all__ = ["MAX_EVENT_BYTES", "VoiceConnection"]
