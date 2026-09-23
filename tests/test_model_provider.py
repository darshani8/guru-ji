import asyncio
import importlib.util
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings
from app.domain.errors import ErrorCode, GuruJiError, PublicError
from app.orchestration.answer_synthesizer import AssistantAnswer, apply_model_wording
from app.providers.anthropic import BEDROCK_FALLBACK_MODEL, FALLBACK_BETA, LATENCY_SENSITIVE_SYSTEM, MIN_MAX_TOKENS, AnthropicProvider
from app.providers.ollama import OllamaProvider

ANTHROPIC_SDK_AVAILABLE = importlib.util.find_spec("anthropic") is not None


class _StaticModel:
    provider_id = "test-model"

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.prompt = None

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        self.prompt = prompt
        self.max_tokens = max_tokens
        if self.error:
            raise self.error
        return self.answer


class ModelProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_posts_non_streaming_bounded_generation_request(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/generate")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "demo-model")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["options"]["num_predict"], 120)
            self.assertIn("approved answer", payload["prompt"])
            return httpx.Response(200, json={"response": "approved answer"})

        provider = OllamaProvider(
            base_url="http://ollama.test:11434",
            model_id="demo-model",
            timeout_seconds=2,
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(await provider.complete("approved answer", max_tokens=120), "approved answer")

    async def test_ollama_translates_provider_failure_without_leaking_details(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(503, text="private provider internals")

        provider = OllamaProvider(
            base_url="http://ollama.test:11434",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(GuruJiError) as context:
            await provider.complete("approved answer")
        self.assertEqual(context.exception.public_error.message, "The configured local model provider is unavailable.")
        self.assertNotIn("private provider internals", str(context.exception))

    async def test_model_wording_preserves_approved_numeric_facts(self):
        model = _StaticModel("There are 1240 students and attendance is 87.4%.")
        answer = AssistantAnswer(
            request_id="req-model",
            status="complete",
            answer="The approved source reports 1240 active students and an aggregate attendance rate of 87.4%.",
        )
        result = await apply_model_wording(answer, model, max_tokens=120)
        self.assertEqual(result.generation_mode, "test-model")
        self.assertEqual(result.answer, model.answer)
        self.assertIn("<approved_answer>", model.prompt)

    async def test_model_output_that_drops_facts_falls_back(self):
        model = _StaticModel("The institution is doing well.")
        answer = AssistantAnswer(
            request_id="req-model",
            status="complete",
            answer="The approved source reports 1240 active students and an aggregate attendance rate of 87.4%.",
        )
        result = await apply_model_wording(answer, model)
        self.assertEqual(result.generation_mode, "deterministic_fallback")
        self.assertEqual(result.answer, answer.answer)
        self.assertTrue(any(item["code"] == "model_output_rejected" for item in result.warnings))

    async def test_provider_failure_returns_deterministic_answer(self):
        model = _StaticModel(error=GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "provider failed", "test")))
        answer = AssistantAnswer(request_id="req-model", status="complete", answer="Approved answer 1240.")
        result = await apply_model_wording(answer, model)
        self.assertEqual(result.generation_mode, "deterministic_fallback")
        self.assertEqual(result.answer, answer.answer)
        self.assertTrue(any(item["code"] == "model_unavailable" for item in result.warnings))

    def test_runtime_selects_ollama_only_when_explicitly_configured(self):
        settings = AppSettings(
            environment="test",
            control_database_url=":memory:",
            model_provider="ollama",
            ollama_base_url="http://127.0.0.1:11434",
        )
        runtime = build_runtime(settings)
        try:
            self.assertIsInstance(runtime.assistant.model, OllamaProvider)
        finally:
            runtime.store.close()


def _message(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type=kind, text=text) for kind, text in blocks],
        usage=SimpleNamespace(input_tokens=40, output_tokens=12, cache_read_input_tokens=0),
    )


class _FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    async def text_stream(self):
        for block in self.message.content:
            if block.type == "text":
                yield block.text

    async def get_final_message(self):
        return self.message


class _FakeMessages:
    """Stands in for AsyncAnthropic().beta.messages and records each request."""

    def __init__(self, message=None, error=None, delay=0.0):
        self.message, self.error, self.delay = message, error, delay
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.message

    def stream(self, **request):
        self.requests.append(request)
        return _FakeStream(self.message)


def _claude(messages, **options):
    return AnthropicProvider(client=SimpleNamespace(beta=SimpleNamespace(messages=messages)), **options)


class ClaudeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_carries_effort_fallbacks_and_room_for_thinking(self):
        messages = _FakeMessages(_message(("thinking", ""), ("fallback", ""), ("text", "There are 1240 students.")))
        provider = _claude(messages, system=LATENCY_SENSITIVE_SYSTEM)
        self.assertEqual(await provider.complete("approved answer", max_tokens=120), "There are 1240 students.")
        request = messages.requests[0]
        self.assertEqual(request["model"], "claude-opus-5")
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertEqual(request["betas"], [FALLBACK_BETA])
        self.assertEqual(request["fallbacks"], "default")
        # Thinking shares max_tokens with the answer, so the caller's small cap is raised.
        self.assertEqual(request["max_tokens"], MIN_MAX_TOKENS)
        self.assertEqual(request["system"], LATENCY_SENSITIVE_SYSTEM)
        self.assertEqual(request["messages"], [{"role": "user", "content": "approved answer"}])

    async def test_models_without_the_default_fallback_mode_do_not_request_it(self):
        messages = _FakeMessages(_message(("text", "ok")))
        await _claude(messages, model_id="claude-sonnet-5", effort="medium").complete("approved answer")
        request = messages.requests[0]
        self.assertNotIn("fallbacks", request)
        self.assertNotIn("betas", request)
        self.assertNotIn("system", request)
        self.assertEqual(request["output_config"], {"effort": "medium"})

    async def test_refused_or_truncated_answers_fall_back_to_the_approved_answer(self):
        answer = AssistantAnswer(request_id="req-claude", status="complete", answer="Approved answer 1240.")
        for stop_reason in ("refusal", "max_tokens"):
            with self.subTest(stop_reason=stop_reason):
                provider = _claude(_FakeMessages(_message(("text", "Partial 1240"), stop_reason=stop_reason)))
                with self.assertLogs("app.providers.anthropic", "WARNING"):
                    result = await apply_model_wording(answer, provider)
                self.assertEqual(result.generation_mode, "deterministic_fallback")
                self.assertEqual(result.answer, answer.answer)

    async def test_a_slow_answer_is_abandoned_at_the_deadline(self):
        provider = _claude(_FakeMessages(_message(("text", "late")), delay=1.0), timeout_seconds=0.05)
        with self.assertLogs("app.providers.anthropic", "WARNING") as logs, self.assertRaises(GuruJiError):
            await provider.complete("approved answer")
        self.assertIn("no answer within 0.05s", logs.output[0])

    @unittest.skipUnless(ANTHROPIC_SDK_AVAILABLE, "the anthropic extra is not installed")
    async def test_sdk_failures_are_named_in_the_log_but_not_to_callers(self):
        import anthropic
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = (
            (anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None), "authentication failed: invalid x-api-key"),
            (anthropic.NotFoundError("model: claude-x", response=httpx2.Response(404, request=request), body=None), "model not found: model: claude-x"),
            (anthropic.APIConnectionError(request=request), "the API could not be reached"),
            (RuntimeError("Could not resolve AWS credentials from session"), "RuntimeError: Could not resolve AWS credentials"),
        )
        for error, reason in cases:
            with self.subTest(reason=reason):
                with self.assertLogs("app.providers.anthropic", "WARNING") as logs, self.assertRaises(GuruJiError) as context:
                    await _claude(_FakeMessages(error=error)).complete("approved answer")
                self.assertIn(reason, logs.output[0])
                self.assertEqual(context.exception.public_error.message, "The configured Claude model is unavailable.")

    @unittest.skipUnless(ANTHROPIC_SDK_AVAILABLE, "the anthropic extra is not installed")
    async def test_the_sdk_sends_the_documented_request_and_the_answer_is_marked_as_claude(self):
        import anthropic
        import httpx2

        sent = {}

        def handler(request):
            sent["beta"] = request.headers.get("anthropic-beta")
            sent["body"] = json.loads(request.content)
            return httpx2.Response(200, json={
                "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5",
                "content": [{"type": "text", "text": "There are 1240 active students."}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 50, "output_tokens": 9},
            })

        client = anthropic.AsyncAnthropic(
            api_key="test-key",
            base_url="https://api.anthropic.com",
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        answer = AssistantAnswer(request_id="req-claude", status="complete", answer="The source reports 1240 active students.")
        result = await apply_model_wording(answer, AnthropicProvider(client=client, system=LATENCY_SENSITIVE_SYSTEM), max_tokens=800)
        self.assertEqual((result.generation_mode, result.answer), ("anthropic", "There are 1240 active students."))
        self.assertEqual(sent["beta"], FALLBACK_BETA)
        self.assertEqual(sent["body"]["fallbacks"], "default")
        self.assertEqual(sent["body"]["output_config"], {"effort": "low"})
        self.assertEqual(sent["body"]["model"], "claude-opus-5")

    async def test_missing_sdk_is_a_provider_failure_not_a_crash(self):
        provider = AnthropicProvider()
        with patch.dict(sys.modules, {"anthropic": None}), self.assertLogs("app.providers.anthropic", "WARNING"):
            with self.assertRaises(GuruJiError):
                await provider.complete("approved answer")

    async def test_stream_yields_text_then_a_final_event_with_usage(self):
        provider = _claude(_FakeMessages(_message(("text", "Hello "), ("text", "there"))))
        events = [event async for event in provider.stream("approved answer")]
        self.assertEqual("".join(event.text for event in events), "Hello there")
        self.assertTrue(events[-1].is_final)
        self.assertEqual(events[-1].usage["output_tokens"], 12)

    async def test_a_refusal_after_streamed_text_is_reported_as_a_failed_spoken_reply(self):
        from app.conversation.streaming import stream_reply

        heard: list[str] = []

        async def on_text(text):
            heard.append(text)

        declined = _claude(_FakeMessages(_message(("text", "Partial answer. "), ("text", "More"), stop_reason="refusal")))
        with self.assertLogs("app.providers.anthropic", "WARNING"):
            outcome = await stream_reply(declined, "prompt", max_tokens=100, first_text_seconds=2, total_seconds=5, on_text=on_text)
        self.assertEqual((outcome.error, outcome.emitted_any), ("provider", True))
        self.assertEqual("".join(heard), "Partial answer. More", "the caller retracts what was already heard")
        heard.clear()
        fine = _claude(_FakeMessages(_message(("fallback", ""), ("text", "All good."))))
        outcome = await stream_reply(fine, "prompt", max_tokens=100, first_text_seconds=2, total_seconds=5, on_text=on_text)
        self.assertEqual((outcome.error, outcome.text, "".join(heard)), (None, "All good.", "All good."))

    def test_effort_and_model_are_validated(self):
        with self.assertRaises(ValueError):
            AnthropicProvider(effort="fast")
        with self.assertRaises(ValueError):
            AnthropicProvider(model_id=" ")
        with self.assertRaises(ValueError):
            AppSettings(model_provider="anthropic", anthropic_planner_effort="fast").ensure_safe_for_production()
        settings = AppSettings(model_provider="anthropic")
        settings.ensure_safe_for_production()
        self.assertEqual((settings.anthropic_model_id, settings.anthropic_effort, settings.anthropic_planner_effort), ("claude-opus-5", "low", "medium"))
        self.assertNotIn("sk-ant-secret", repr(AppSettings(model_provider="anthropic", anthropic_api_key="sk-ant-secret")))

    def test_runtime_uses_quick_claude_for_answers_and_a_deeper_one_for_planning(self):
        settings = AppSettings(
            environment="test",
            control_database_url=":memory:",
            model_provider="anthropic",
            agent_planner="model",
            platform_enabled=True,
        )
        runtime = build_runtime(settings)
        try:
            answers = runtime.assistant.model
            planner = runtime.platform.agent.model_planner.model
            self.assertIsInstance(answers, AnthropicProvider)
            self.assertEqual((answers.effort, answers.system), ("low", LATENCY_SENSITIVE_SYSTEM))
            self.assertIsInstance(planner, AnthropicProvider)
            self.assertEqual((planner.effort, planner.system), ("medium", None))
        finally:
            runtime.close()


def _bedrock_reply(model, *, refused=False):
    return {
        "id": "msg_bedrock", "type": "message", "role": "assistant", "model": model,
        "content": [] if refused else [{"type": "text", "text": "There are 1240 active students."}],
        "stop_reason": "refusal" if refused else "end_turn", "stop_sequence": None,
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": None, "fallback_credit_token": "tok_test", "fallback_has_prefill_claim": False} if refused else None,
        "usage": {"input_tokens": 50, "output_tokens": 0 if refused else 9},
    }


@unittest.skipUnless(ANTHROPIC_SDK_AVAILABLE, "the anthropic extra is not installed")
class ClaudeOnBedrockTests(unittest.IsolatedAsyncioTestCase):
    """Claude in Amazon Bedrock through the real SDK client over a mock transport.

    A Bedrock bearer token stands in for the AWS credential chain so no request
    needs SigV4 signing.
    """

    def setUp(self):
        patcher = patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "bedrock-test-token"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _provider(self, handler, **options):
        import httpx2

        return AnthropicProvider(platform="bedrock", aws_region="ap-south-1", transport=httpx2.MockTransport(handler), **options)

    def test_bedrock_ids_carry_the_provider_prefix(self):
        self.assertEqual(AnthropicProvider(platform="bedrock").model_id, "anthropic.claude-opus-5")
        self.assertEqual(AnthropicProvider(platform="bedrock", model_id="global.anthropic.claude-opus-5").model_id, "global.anthropic.claude-opus-5")
        self.assertEqual(AnthropicProvider(platform="bedrock").provider_id, "bedrock")
        with self.assertRaises(ValueError):
            AnthropicProvider(platform="vertex")

    async def test_a_declined_request_is_retried_client_side_on_the_fallback_model(self):
        import httpx2

        sent = []

        def handler(request):
            body = json.loads(request.content)
            sent.append((request.url, body))
            return httpx2.Response(200, json=_bedrock_reply(body["model"], refused=body["model"] == "anthropic.claude-opus-5"))

        answer = AssistantAnswer(request_id="req-bedrock", status="complete", answer="The source reports 1240 active students.")
        with self.assertNoLogs("anthropic.lib.middleware", "WARNING"):
            result = await apply_model_wording(answer, self._provider(handler, system=LATENCY_SENSITIVE_SYSTEM))
        self.assertEqual((result.generation_mode, result.answer), ("bedrock", "There are 1240 active students."))
        (first_url, first), (_, second) = sent
        self.assertEqual((first_url.host, first_url.path), ("bedrock-mantle.ap-south-1.api.aws", "/anthropic/v1/messages"))
        # Bedrock has no server-side fallbacks: the first request carries none.
        self.assertNotIn("fallbacks", first)
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertEqual(second["model"], BEDROCK_FALLBACK_MODEL)

    async def test_an_account_without_model_access_is_named_in_the_log(self):
        import httpx2

        def handler(request):
            del request
            return httpx2.Response(403, json={"message": "anthropic.claude-opus-5 is not available for this account."})

        with self.assertLogs("app.providers.anthropic", "WARNING") as logs, self.assertRaises(GuruJiError) as context:
            await self._provider(handler).complete("approved answer")
        self.assertIn("access denied", logs.output[0])
        self.assertIn("not available for this account", logs.output[0])
        self.assertEqual(context.exception.public_error.message, "The configured Claude model is unavailable.")

    def test_bedrock_needs_a_region_and_the_runtime_wires_both_roles(self):
        with self.assertRaises(ValueError):
            AppSettings(model_provider="bedrock").ensure_safe_for_production()
        with patch.dict(os.environ, {"GURU_MODEL_PROVIDER": "bedrock", "AWS_REGION": "ap-south-1"}):
            self.assertEqual(AppSettings.from_env().bedrock_region, "ap-south-1")
        settings = AppSettings(
            environment="test",
            control_database_url=":memory:",
            model_provider="bedrock",
            bedrock_region="ap-south-1",
            agent_planner="model",
            platform_enabled=True,
        )
        runtime = build_runtime(settings)
        try:
            answers = runtime.assistant.model
            planner = runtime.platform.agent.model_planner.model
            self.assertEqual((answers.platform, answers.model_id, answers.aws_region, answers.effort), ("bedrock", "anthropic.claude-opus-5", "ap-south-1", "low"))
            self.assertEqual((planner.platform, planner.effort, planner.system), ("bedrock", "medium", None))
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
