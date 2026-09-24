"""Spoken replies: Amazon Polly (Kajal, Indian English and Hindi) or the browser's own voice.

Polly is called from a dedicated thread pool sized to the configured
concurrency, so a slow or throttled Polly can never occupy the event loop or
the default executor. Any failure returns ``None``: the client then speaks the
same sentence with the best local voice, so a Polly outage costs accent, never
the answer. Polly has no Kannada voice; Kannada is left to the device.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("saffron.voice.tts")

# Polly bills and limits by characters of input text per request.
POLLY_MAX_TEXT_CHARS = 2900
MAX_AUDIO_BYTES = 2_000_000
# Bilingual Indian voices read English and Hindi (Devanagari or romanised).
_BILINGUAL_INDIAN_VOICES = frozenset({"kajal", "aditi"})


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    audio: bytes = field(repr=False)
    language: str
    voice: str
    format: str = "mp3"
    sample_rate: int = 24_000


class SpeechSynthesizer(Protocol):
    provider: str

    def supports(self, language: str) -> bool: ...

    async def synthesize(self, text: str, *, language: str, cacheable: bool = False) -> SynthesizedSpeech | None: ...

    def describe(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class NullSynthesizer:
    """The browser speaks: no server audio is produced."""

    provider = "browser"

    def supports(self, language: str) -> bool:
        return False

    async def synthesize(self, text: str, *, language: str, cacheable: bool = False) -> SynthesizedSpeech | None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "voice": None, "languages": []}

    def close(self) -> None:
        return None


class PollySynthesizer:
    provider = "polly"

    def __init__(
        self,
        *,
        voice_id: str = "Kajal",
        engine: str = "neural",
        region: str | None = None,
        timeout_seconds: float = 4.0,
        max_concurrency: int = 8,
        sample_rate: int = 24_000,
        client: Any | None = None,
        cache_size: int = 64,
    ) -> None:
        if timeout_seconds <= 0 or max_concurrency <= 0:
            raise ValueError("Polly limits must be positive")
        self.voice_id = voice_id
        self.engine = engine
        self.region = region
        self.timeout_seconds = timeout_seconds
        self.sample_rate = sample_rate
        self._client = client
        self.max_concurrency = max_concurrency
        self._executor = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="polly")
        # Waiting callers beyond the pool size queue here, not in the executor,
        # so a cancelled turn never leaves work behind it in the queue. One
        # semaphore per event loop: asyncio primitives are bound to a loop.
        self._slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()
        self._cache: OrderedDict[tuple[str, str], SynthesizedSpeech] = OrderedDict()
        self._cache_size = cache_size
        self.languages: tuple[str, ...] = ("en-IN", "hi-IN") if voice_id.lower() in _BILINGUAL_INDIAN_VOICES else ("en-IN",)

    def supports(self, language: str) -> bool:
        return language in self.languages

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "voice": self.voice_id, "engine": self.engine, "languages": list(self.languages)}

    def _polly(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "polly",
                region_name=self.region,
                config=Config(connect_timeout=2, read_timeout=self.timeout_seconds, retries={"max_attempts": 2, "mode": "standard"}),
            )
        return self._client

    def _call(self, text: str, language: str) -> bytes:
        response = self._polly().synthesize_speech(
            Engine=self.engine,
            LanguageCode=language,
            OutputFormat="mp3",
            SampleRate=str(self.sample_rate),
            Text=text,
            TextType="text",
            VoiceId=self.voice_id,
        )
        stream = response["AudioStream"]
        try:
            audio = stream.read(MAX_AUDIO_BYTES + 1)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("Polly returned more audio than a spoken sentence needs")
        return audio

    async def synthesize(self, text: str, *, language: str, cacheable: bool = False) -> SynthesizedSpeech | None:
        spoken = " ".join(text.split())[:POLLY_MAX_TEXT_CHARS]
        if not spoken or not self.supports(language):
            return None
        key = (language, spoken)
        if cacheable and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        loop = asyncio.get_running_loop()
        slots = self._slots.get(loop)
        if slots is None:
            slots = self._slots[loop] = asyncio.Semaphore(self.max_concurrency)
        try:
            async with slots:
                audio = await asyncio.wait_for(loop.run_in_executor(self._executor, self._call, spoken, language), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the browser voice
            logger.warning("Polly speech unavailable (%s); the browser voice speaks instead", _describe(exc))
            return None
        if not audio:
            return None
        speech = SynthesizedSpeech(audio=audio, language=language, voice=self.voice_id, sample_rate=self.sample_rate)
        if cacheable:
            # Only fixed phrases (fillers, greetings) are cached, never a person's answer.
            self._cache[key] = speech
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return speech

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _describe(exc: Exception) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    if code:
        return f"{type(exc).__name__}: {code}"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timed out"
    return type(exc).__name__


def build_synthesizer(settings: Any) -> SpeechSynthesizer:
    if settings.voice_tts_provider == "polly":
        return PollySynthesizer(
            voice_id=settings.voice_polly_voice_id,
            engine=settings.voice_polly_engine,
            region=settings.voice_polly_region,
            timeout_seconds=settings.voice_tts_timeout_seconds,
            max_concurrency=settings.voice_tts_max_concurrency,
        )
    return NullSynthesizer()


__all__ = ["NullSynthesizer", "PollySynthesizer", "SpeechSynthesizer", "SynthesizedSpeech", "build_synthesizer"]
