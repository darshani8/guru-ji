"""Check the real voice services before a demo: Polly, the conversation model and web search.

A wrong key or a missing IAM permission does not break Agent Saffron; it quietly
falls back to the browser voice, fixed replies or no web search. Run this in
the deployed task, with the same environment as the API, to see which one is
in effect:

    PYTHONPATH=apps/api python scripts/voice_preflight.py

It makes one small call to each configured service, sends no institution data
and prints no secrets. It exits 1 if a configured service does not work.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from time import monotonic

from app.api.dependencies import _build_conversation_model, _build_model
from app.config.settings import AppSettings
from app.conversation.streaming import stream_reply
from app.internet_intelligence.search import IntelligenceSearchUnavailable, TavilyIntelligenceSearchProvider
from app.voice.tts import build_synthesizer

VOICE_SAMPLES = (("en-IN", "Hello, I am Agent Saffron. How can I help you today?"), ("hi-IN", "नमस्ते, मैं एजेंट सैफ्रन हूँ।"))
MODEL_PROMPT = "Greet a college student in one short, friendly sentence."
WEB_QUERY = "weather in Bengaluru today"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str  # OK | SKIP | WARN | FAIL
    detail: str


async def check_voice(settings: AppSettings, synthesizer) -> list[Check]:
    if settings.voice_tts_provider != "polly":
        return [Check("voice", "SKIP", "SAFFRON_VOICE_TTS_PROVIDER is browser: the device's own voice speaks")]
    checks = []
    for language, text in VOICE_SAMPLES:
        speech = await synthesizer.synthesize(text, language=language)
        if speech is not None and speech.audio:
            checks.append(Check("voice", "OK", f"Polly {settings.voice_polly_voice_id} {language}: {len(speech.audio)} bytes of audio"))
        else:
            region = settings.voice_polly_region or "the default region"
            checks.append(Check("voice", "FAIL", f"no Polly audio for {language}; check the AWS credentials and polly:SynthesizeSpeech in {region} (reason logged above)"))
    return checks


async def check_model(settings: AppSettings, model) -> Check:
    if not settings.conversation_enabled:
        return Check("model", "WARN", "SAFFRON_CONVERSATION_ENABLED is off: questions the agent cannot map get a fixed reply")
    if model is None:
        return Check("model", "WARN", f"SAFFRON_MODEL_PROVIDER is {settings.model_provider}: conversation uses fixed replies")
    started = monotonic()
    first: list[float] = []

    async def on_text(_text: str) -> None:
        if not first:
            first.append(monotonic() - started)

    outcome = await stream_reply(
        model, MODEL_PROMPT, max_tokens=60,
        first_text_seconds=settings.conversation_first_text_seconds, total_seconds=settings.conversation_stream_seconds,
        on_text=on_text, detect_search=False,
    )
    if outcome.ok and outcome.text.strip():
        return Check("model", "OK", f"{settings.model_provider}: first words after {first[0] * 1000:.0f} ms, whole reply after {(monotonic() - started) * 1000:.0f} ms")
    return Check("model", "FAIL", f"{settings.model_provider} gave {outcome.error or 'an empty reply'}; check the model id, key or Bedrock access")


def web_provider(settings: AppSettings) -> TavilyIntelligenceSearchProvider | None:
    if not settings.assistant_web_search or settings.web_search_provider != "tavily" or not settings.web_search_api_key:
        return None
    return TavilyIntelligenceSearchProvider(
        api_key=settings.web_search_api_key, endpoint=settings.web_search_endpoint,
        timeout_seconds=settings.web_search_timeout_seconds, exclude_domains=settings.assistant_web_exclude_domains,
    )


async def check_web(settings: AppSettings, provider) -> Check:
    if provider is None:
        if not settings.assistant_web_search:
            return Check("web", "WARN", "SAFFRON_ASSISTANT_WEB_SEARCH is off: \"search the internet\" is answered without the web")
        return Check("web", "WARN", "web search is off: set SAFFRON_WEB_SEARCH_PROVIDER=tavily and SAFFRON_WEB_SEARCH_API_KEY")
    try:
        hits = await provider.search(WEB_QUERY, max_results=3)
    except IntelligenceSearchUnavailable as exc:
        return Check("web", "FAIL", f"Tavily: {exc}; check SAFFRON_WEB_SEARCH_API_KEY and outbound access to {settings.web_search_endpoint}")
    if not hits:
        return Check("web", "WARN", "Tavily answered but found nothing for a simple query")
    return Check("web", "OK", f"Tavily: {len(hits)} results")


async def run(settings: AppSettings, *, synthesizer, model, web) -> list[Check]:
    checks = await check_voice(settings, synthesizer)
    checks.append(await check_model(settings, model))
    checks.append(await check_web(settings, web))
    return checks


def main(settings: AppSettings | None = None, *, synthesizer=None, model=None, web=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="  %(name)s: %(message)s")
    settings = settings or AppSettings.from_env()
    synthesizer = synthesizer or build_synthesizer(settings)
    if model is None:
        model = _build_conversation_model(settings, _build_model(settings))
    web = web if web is not None else web_provider(settings)
    try:
        checks = asyncio.run(run(settings, synthesizer=synthesizer, model=model, web=web))
    finally:
        synthesizer.close()
    for check in checks:
        print(f"[{check.status:>4}] {check.name:<5} {check.detail}")
    if any(check.status == "FAIL" for check in checks):
        print("VOICE_PREFLIGHT_FAILED")
        return 1
    print("VOICE_PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
