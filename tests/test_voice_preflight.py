import contextlib
import importlib.util
import io
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from app.config.settings import AppSettings
from app.domain.errors import ErrorCode, GuruJiError, PublicError
from app.internet_intelligence.search import IntelligenceSearchUnavailable, SearchHit
from app.providers.model_base import ModelEvent
from app.voice.tts import SynthesizedSpeech

_SPEC = importlib.util.spec_from_file_location("voice_preflight", Path(__file__).resolve().parents[1] / "scripts" / "voice_preflight.py")
preflight = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = preflight  # its dataclass looks the module up while it loads
_SPEC.loader.exec_module(preflight)


class _Synthesizer:
    def __init__(self, audio=b"mp3"):
        self.audio = audio
        self.languages = []
        self.closed = False

    async def synthesize(self, text, *, language, cacheable=False):
        self.languages.append(language)
        return SynthesizedSpeech(audio=self.audio, language=language, voice="Kajal", sample_rate=24000) if self.audio else None

    def close(self):
        self.closed = True


class _Model:
    def __init__(self, pieces, *, fail=False):
        self.pieces = pieces
        self.fail = fail

    def stream(self, prompt, *, max_tokens=800):
        return self._stream()

    async def _stream(self):
        for piece in self.pieces:
            yield ModelEvent(type="delta", text=piece)
        if self.fail:
            raise GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "declined", "preflight"))


class _Web:
    def __init__(self, hits=None, error=None):
        self.hits = hits or ()
        self.error = error

    async def search(self, query, *, max_results):
        if self.error:
            raise self.error
        return self.hits


def _run(settings, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = preflight.main(settings, **kwargs)
    return code, out.getvalue()


class VoicePreflightTests(unittest.TestCase):
    def test_default_settings_report_what_falls_back_and_pass(self):
        synthesizer = _Synthesizer()
        code, out = _run(AppSettings(), synthesizer=synthesizer)
        self.assertEqual(code, 0)
        self.assertIn("[SKIP] voice", out)
        self.assertIn("[WARN] model", out)
        self.assertIn("[WARN] web", out)
        self.assertTrue(out.rstrip().endswith("VOICE_PREFLIGHT_OK"))
        self.assertEqual(synthesizer.languages, [])
        self.assertTrue(synthesizer.closed)

    def test_working_services_are_reported_ok(self):
        settings = replace(AppSettings(), voice_tts_provider="polly", model_provider="anthropic")
        hit = SearchHit(url="https://example.org/weather", title="Weather", snippet="Sunny", published_at=None, source_name="example.org")
        synthesizer = _Synthesizer()
        code, out = _run(settings, synthesizer=synthesizer, model=_Model(["Hello there, ", "welcome to college!"]), web=_Web(hits=(hit,)))
        self.assertEqual(code, 0, out)
        self.assertEqual(synthesizer.languages, ["en-IN", "hi-IN"])
        self.assertIn("[  OK] voice Polly Kajal en-IN", out)
        self.assertIn("[  OK] model anthropic: first words after", out)
        self.assertIn("[  OK] web   Tavily: 1 results", out)

    def test_missing_polly_permission_fails(self):
        settings = replace(AppSettings(), voice_tts_provider="polly", voice_polly_region="ap-south-1")
        code, out = _run(settings, synthesizer=_Synthesizer(audio=b""))
        self.assertEqual(code, 1)
        self.assertIn("polly:SynthesizeSpeech in ap-south-1", out)
        self.assertTrue(out.rstrip().endswith("VOICE_PREFLIGHT_FAILED"))

    def test_a_model_that_fails_or_a_search_that_fails_is_reported(self):
        settings = replace(AppSettings(), model_provider="anthropic")
        code, out = _run(settings, synthesizer=_Synthesizer(), model=_Model(["Hello"], fail=True), web=_Web(error=IntelligenceSearchUnavailable("search provider timed out")))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] model anthropic gave provider", out)
        self.assertIn("[FAIL] web   Tavily: search provider timed out", out)


if __name__ == "__main__":
    unittest.main()
