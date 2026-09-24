"""Speech text preparation and the Polly synthesizer, with a fake Polly client."""

from __future__ import annotations

import asyncio
import io
import unittest

from app.config.settings import AppSettings
from app.voice.speech_text import ON_SCREEN, SentenceStreamer, speech_chunks, split_sentences, to_speech
from app.voice.tts import NullSynthesizer, PollySynthesizer, build_synthesizer


class _FakePolly:
    def __init__(self, error: Exception | None = None, audio: bytes = b"ID3-mp3") -> None:
        self.calls: list[dict] = []
        self.error = error
        self.audio = audio

    def synthesize_speech(self, **request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return {"AudioStream": io.BytesIO(self.audio), "ContentType": "audio/mpeg"}


class _Throttled(Exception):
    response = {"Error": {"Code": "ThrottlingException"}}


class SpeechTextTests(unittest.TestCase):
    def test_links_paths_markers_and_markdown_are_not_read_aloud(self) -> None:
        text = "**Result:** 12 students are below 75% [1]. See https://example.edu/x. Download: /v1/reports/r1/download"
        self.assertEqual(to_speech(text), "Result: 12 students are below 75%. See.")

    def test_long_name_lists_are_shortened(self) -> None:
        names = "; ".join(f"Student {index} (MBA{index:03d}, 60%)" for index in range(1, 9))
        spoken = to_speech(f"8 students found. Names: {names} and 4 more.")
        self.assertIn("Student 5", spoken)
        self.assertNotIn("Student 6", spoken)
        self.assertIn("and 7 more on your screen", spoken)
        self.assertNotIn("MBA001", spoken)

    def test_sentences_split_without_breaking_decimals_titles_or_degrees(self) -> None:
        text = "Attendance is 75.5 percent. Dr. Rao teaches B.Tech students! Is that fine? हाँ। ठीक है।"
        self.assertEqual(split_sentences(text), ["Attendance is 75.5 percent.", "Dr. Rao teaches B.Tech students!", "Is that fine?", "हाँ।", "ठीक है।"])

    def test_chunks_start_short_and_stop_at_the_limit(self) -> None:
        text = " ".join(f"This is sentence number {index} of a long answer." for index in range(1, 60))
        chunks = speech_chunks(text, max_chars=400, first_chars=90)
        self.assertLessEqual(len(chunks[0]), 90)
        self.assertEqual(chunks[-1], ON_SCREEN["en-IN"])
        self.assertLessEqual(sum(len(chunk) for chunk in chunks[:-1]), 460)
        self.assertEqual(speech_chunks("नमस्ते। आप कैसे हैं?", language="hi-IN"), ["नमस्ते। आप कैसे हैं?"])
        self.assertEqual(speech_chunks(""), [])


def _stream(chunks, **options):
    streamer = SentenceStreamer(**options)
    steps = [streamer.feed(chunk) for chunk in chunks]
    steps.append(streamer.flush())
    return steps


class SentenceStreamerTests(unittest.TestCase):
    def test_sentences_are_released_as_soon_as_they_end(self) -> None:
        steps = _stream(["Attendance is 75.", "5 percent today. Dr. Rao teaches B.Tech", " students [1]. Sure. ", "OK then.", " Bye."])
        self.assertEqual(steps[0], [], "75. may still become 75.5")
        self.assertEqual(steps[1], ["Attendance is 75.5 percent today."])
        self.assertEqual(steps[2], ["Dr. Rao teaches B.Tech students."], "titles and degrees stay whole; citation markers are not spoken")
        self.assertEqual(steps[-1], ["Sure. OK then. Bye."], "short sentences join the next")

    def test_hindi_danda_ends_a_sentence(self) -> None:
        steps = _stream(["मैं आपकी मदद कर सकती हूँ, बताइए। ", "आज क्या जानना है?"], language="hi-IN")
        self.assertEqual(steps[0], ["मैं आपकी मदद कर सकती हूँ, बताइए।"])
        self.assertEqual(steps[-1], ["आज क्या जानना है?"])

    def test_a_long_opening_clause_is_spoken_at_a_comma(self) -> None:
        opening = "This is a long opening clause that keeps going, and going, with plenty of words in it, and it does not stop yet because"
        steps = _stream([opening, " the model is still writing."], first_chars=60)
        self.assertTrue(steps[0], "speech starts before the first full stop")
        self.assertLessEqual(len(steps[0][0]), 100)
        self.assertEqual(" ".join(steps[0] + steps[-1]).replace("  ", " "), opening + " the model is still writing.")

    def test_speech_stops_at_the_limit(self) -> None:
        steps = _stream(["One two three four five six. " * 10], max_chars=80)
        spoken = [piece for step in steps for piece in step]
        self.assertEqual(spoken[-1], ON_SCREEN["en-IN"])
        self.assertLessEqual(sum(len(piece) for piece in spoken[:-1]), 80)
        streamer = SentenceStreamer()
        self.assertEqual(streamer.feed(""), [])
        self.assertEqual(streamer.flush(), [])


class PollyTests(unittest.IsolatedAsyncioTestCase):
    async def test_kajal_neural_request(self) -> None:
        client = _FakePolly()
        polly = PollySynthesizer(client=client, region="ap-south-1")
        speech = await polly.synthesize("Namaste, how can I help?", language="en-IN")
        self.assertEqual(speech.audio, b"ID3-mp3")
        self.assertEqual(client.calls[0], {
            "Engine": "neural", "LanguageCode": "en-IN", "OutputFormat": "mp3", "SampleRate": "24000",
            "Text": "Namaste, how can I help?", "TextType": "text", "VoiceId": "Kajal",
        })
        hindi = await polly.synthesize("नमस्ते, मैं आपकी मदद कर सकती हूँ।", language="hi-IN")
        self.assertEqual(hindi.language, "hi-IN")
        polly.close()

    async def test_kannada_and_failures_fall_back_to_the_browser(self) -> None:
        client = _FakePolly()
        polly = PollySynthesizer(client=client)
        self.assertIsNone(await polly.synthesize("ನಮಸ್ಕಾರ", language="kn-IN"))
        self.assertEqual(client.calls, [], "Polly has no Kannada voice, so it is never asked")
        throttled = PollySynthesizer(client=_FakePolly(error=_Throttled()))
        with self.assertLogs("saffron.voice.tts", "WARNING") as logs:
            self.assertIsNone(await throttled.synthesize("hello", language="en-IN"))
        self.assertIn("ThrottlingException", logs.output[0])
        oversized = PollySynthesizer(client=_FakePolly(audio=b"x" * 2_000_001))
        with self.assertLogs("saffron.voice.tts", "WARNING"):
            self.assertIsNone(await oversized.synthesize("hello", language="en-IN"))
        for item in (polly, throttled, oversized):
            item.close()

    async def test_a_slow_polly_times_out(self) -> None:
        class _Slow(_FakePolly):
            def synthesize_speech(self, **request):
                import time

                time.sleep(0.5)
                return super().synthesize_speech(**request)

        polly = PollySynthesizer(client=_Slow(), timeout_seconds=0.05)
        polly.timeout_seconds = 0.05
        with self.assertLogs("saffron.voice.tts", "WARNING"):
            started = asyncio.get_running_loop().time()
            self.assertIsNone(await polly.synthesize("hello", language="en-IN"))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.4)
        polly.close()

    async def test_only_fixed_phrases_are_cached(self) -> None:
        client = _FakePolly()
        polly = PollySynthesizer(client=client)
        await polly.synthesize("One moment.", language="en-IN", cacheable=True)
        await polly.synthesize("One moment.", language="en-IN", cacheable=True)
        await polly.synthesize("Your balance is 1200.", language="en-IN")
        await polly.synthesize("Your balance is 1200.", language="en-IN")
        self.assertEqual(len(client.calls), 3)
        polly.close()

    async def test_the_browser_setting_produces_no_audio(self) -> None:
        null = NullSynthesizer()
        self.assertIsNone(await null.synthesize("hello", language="en-IN"))
        self.assertEqual(null.describe()["provider"], "browser")


class SynthesizerSettingsTests(unittest.TestCase):
    def test_settings_choose_the_synthesizer(self) -> None:
        self.assertIsInstance(build_synthesizer(AppSettings()), NullSynthesizer)
        polly = build_synthesizer(AppSettings(voice_tts_provider="polly", voice_polly_region="ap-south-1"))
        self.assertIsInstance(polly, PollySynthesizer)
        self.assertEqual(polly.describe(), {"provider": "polly", "voice": "Kajal", "engine": "neural", "languages": ["en-IN", "hi-IN"]})
        polly.close()

    def test_polly_settings_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "SAFFRON_VOICE_POLLY_REGION"):
            AppSettings(voice_tts_provider="polly", voice_polly_region=None).ensure_safe_for_production()
        with self.assertRaisesRegex(ValueError, "SAFFRON_VOICE_POLLY_ENGINE"):
            AppSettings(voice_tts_provider="polly", voice_polly_region="ap-south-1", voice_polly_engine="turbo").ensure_safe_for_production()
        with self.assertRaisesRegex(ValueError, "SAFFRON_VOICE_TTS_PROVIDER"):
            AppSettings(voice_tts_provider="elevenlabs").ensure_safe_for_production()
        with self.assertRaisesRegex(ValueError, "SAFFRON_VOICE_IDLE_TIMEOUT_SECONDS"):
            AppSettings(voice_idle_timeout_seconds=5).ensure_safe_for_production()
        AppSettings(voice_tts_provider="polly", voice_polly_region="ap-south-1").ensure_safe_for_production()


if __name__ == "__main__":
    unittest.main()
