"""Amazon Nova (and other non-Claude Bedrock models) through the Converse API, over a stubbed boto3 client."""

import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from app.api.dependencies import _build_conversation_model, _build_model, _build_open_task_model, _build_planner_model, build_runtime
from app.config.legacy_env import adopt_legacy_names
from app.config.settings import AppSettings
from app.conversation.streaming import stream_reply
from app.domain.errors import AgentSaffronError
from app.orchestration.answer_synthesizer import AssistantAnswer, apply_model_wording
from app.providers.anthropic import LATENCY_SENSITIVE_SYSTEM, AnthropicProvider
from app.providers.bedrock_converse import MIN_MAX_TOKENS, BedrockConverseModel, is_claude_model

NOVA = "apac.amazon.nova-pro-v1:0"
CLAUDE = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
PUBLIC_MESSAGE = "The configured Bedrock model is unavailable."


class _ClientError(Exception):
    """Shaped like botocore's ClientError: the service's code and message are in ``response``."""

    def __init__(self, code, message):
        super().__init__(f"An error occurred ({code}) when calling the Converse operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def _reply(text="There are 1240 active students.", stop_reason="end_turn"):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}] if text else []}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 40, "outputTokens": 9, "totalTokens": 49},
        "metrics": {"latencyMs": 300},
    }


def _events(*texts, stop_reason="end_turn"):
    return [
        {"messageStart": {"role": "assistant"}},
        *({"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}} for text in texts),
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": stop_reason}},
        {"metadata": {"usage": {"inputTokens": 40, "outputTokens": 9, "totalTokens": 49}, "metrics": {"latencyMs": 250}}},
    ]


class _EventStream:
    """Shaped like botocore's EventStream: iterated on a worker thread, closed when done."""

    def __init__(self, events, error=None):
        self.events, self.error = events, error
        self.closed = False
        self.threads = set()

    def __iter__(self):
        for event in self.events:
            self.threads.add(threading.current_thread().name)
            yield event
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class _FakeBedrock:
    """Stands in for boto3.client("bedrock-runtime") and records each request."""

    def __init__(self, reply=None, *, stream=None, error=None, delay=0.0):
        self.reply, self.stream, self.error, self.delay = reply, stream, error, delay
        self.requests = []
        self.threads = []

    def _record(self, operation, request):
        self.requests.append((operation, request))
        self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error

    def converse(self, **request):
        self._record("converse", request)
        return self.reply

    def converse_stream(self, **request):
        self._record("converse_stream", request)
        return {"stream": self.stream}


def _nova(client, **options):
    return BedrockConverseModel(model_id=options.pop("model_id", NOVA), aws_region="ap-south-1", client=client, **options)


class BedrockConverseTests(unittest.IsolatedAsyncioTestCase):
    def test_only_non_claude_ids_use_converse(self):
        for model_id in ("claude-haiku-4-5", "anthropic.claude-opus-5", CLAUDE, "apac.anthropic.claude-sonnet-4-20250514-v1:0",
                         "arn:aws:bedrock:ap-south-1:123456789012:inference-profile/apac.anthropic.claude-sonnet-4-20250514-v1:0",
                         "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/a1b2c3d4"):
            self.assertTrue(is_claude_model(model_id), model_id)
        for model_id in (NOVA, "apac.amazon.nova-lite-v1:0", "amazon.nova-micro-v1:0", "us.meta.llama3-3-70b-instruct-v1:0",
                         "arn:aws:bedrock:ap-south-1::foundation-model/amazon.nova-pro-v1:0"):
            self.assertFalse(is_claude_model(model_id), model_id)

    async def test_complete_sends_the_converse_request_off_the_event_loop(self):
        client = _FakeBedrock(_reply())
        model = _nova(client, system="Answer briefly.")
        self.assertEqual(await model.complete("approved answer", max_tokens=700), "There are 1240 active students.")
        (operation, request), = client.requests
        self.assertEqual(operation, "converse")
        self.assertEqual(request, {
            "modelId": NOVA,
            "messages": [{"role": "user", "content": [{"text": "approved answer"}]}],
            "inferenceConfig": {"maxTokens": MIN_MAX_TOKENS},
            "system": [{"text": "Answer briefly."}],
        })
        self.assertTrue(client.threads[0].startswith("bedrock-converse"))
        self.assertEqual((model.provider_id, model.platform, model.model_id), ("bedrock", "bedrock", NOVA))
        self.assertTrue(model.capabilities.supports_streaming)
        with self.assertRaises(ValueError):
            await model.complete(" ")
        with self.assertRaises(ValueError):
            BedrockConverseModel(model_id=" ")

    async def test_the_answer_is_worded_and_marked_as_bedrock(self):
        answer = AssistantAnswer(request_id="req-nova", status="complete", answer="The source reports 1240 active students.")
        result = await apply_model_wording(answer, _nova(_FakeBedrock(_reply())))
        self.assertEqual((result.generation_mode, result.answer), ("bedrock", "There are 1240 active students."))

    async def test_cut_off_filtered_or_empty_answers_are_provider_failures(self):
        for reply in (_reply(stop_reason="max_tokens"), _reply(stop_reason="guardrail_intervened"), _reply(stop_reason="content_filtered"), _reply(text="")):
            with self.subTest(stop_reason=reply["stopReason"]), self.assertLogs("app.providers.bedrock_converse", "WARNING"):
                with self.assertRaises(AgentSaffronError) as context:
                    await _nova(_FakeBedrock(reply)).complete("approved answer")
                self.assertEqual(context.exception.public_error.message, PUBLIC_MESSAGE)

    async def test_a_slow_answer_is_abandoned_at_the_deadline(self):
        model = _nova(_FakeBedrock(_reply(), delay=0.5), timeout_seconds=0.05)
        started = time.monotonic()
        with self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs, self.assertRaises(AgentSaffronError):
            await model.complete("approved answer")
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertIn("no answer within 0.05s", logs.output[0])

    async def test_bedrock_errors_are_named_in_the_log_but_not_to_callers(self):
        cases = (
            (_ClientError("AccessDeniedException", "Model access is denied due to INVALID_PAYMENT_INSTRUMENT."), "access denied: Model access is denied due to INVALID_PAYMENT_INSTRUMENT"),
            (_ClientError("ThrottlingException", "Too many requests"), "rate limited"),
            (_ClientError("ValidationException", "The provided model identifier is invalid."), "request rejected"),
            (_ClientError("ResourceNotFoundException", "Model not found"), "model not found"),
            (type("NoCredentialsError", (Exception,), {})("Unable to locate credentials"), "NoCredentialsError: Unable to locate credentials"),
        )
        for error, reason in cases:
            with self.subTest(reason=reason), self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs:
                with self.assertRaises(AgentSaffronError) as context:
                    await _nova(_FakeBedrock(error=error)).complete("approved answer")
                self.assertIn(reason, logs.output[0])
                self.assertEqual(context.exception.public_error.message, PUBLIC_MESSAGE)
                self.assertNotIn("INVALID_PAYMENT", str(context.exception.public_error.message))

    async def test_missing_boto3_is_a_provider_failure_not_a_crash(self):
        with patch.dict(sys.modules, {"boto3": None}), self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs:
            with self.assertRaises(AgentSaffronError):
                await BedrockConverseModel(model_id=NOVA).complete("approved answer")
        self.assertIn("boto3 is not installed", logs.output[0])

    async def test_stream_yields_text_then_a_final_event_with_usage(self):
        stream = _EventStream(_events("There are ", "1240 active", " students."))
        client = _FakeBedrock(stream=stream)
        events = [event async for event in _nova(client).stream("approved answer", max_tokens=700)]
        self.assertEqual([event.text for event in events], ["There are ", "1240 active", " students.", ""])
        self.assertEqual([event.is_final for event in events], [False, False, False, True])
        self.assertEqual(events[-1].usage, {"input_tokens": 40, "output_tokens": 9})
        self.assertEqual({(event.provider_id, event.model_id) for event in events}, {("bedrock", NOVA)})
        (operation, request), = client.requests
        self.assertEqual((operation, request["inferenceConfig"]), ("converse_stream", {"maxTokens": MIN_MAX_TOKENS}))
        self.assertTrue(stream.closed)
        self.assertTrue(all(name.startswith("bedrock-converse") for name in stream.threads))

    async def test_spoken_replies_stream_and_a_cut_off_after_text_is_a_failed_reply(self):
        spoken = []

        async def on_text(text):
            spoken.append(text)

        outcome = await stream_reply(_nova(_FakeBedrock(stream=_EventStream(_events("Hello! ", "How can I help?")))), "hi", max_tokens=700,
                                     first_text_seconds=2, total_seconds=5, on_text=on_text)
        self.assertTrue(outcome.ok)
        self.assertEqual("".join(spoken), "Hello! How can I help?")
        with self.assertLogs("app.providers.bedrock_converse", "WARNING"):
            outcome = await stream_reply(_nova(_FakeBedrock(stream=_EventStream(_events("Partial 1240", stop_reason="max_tokens")))), "hi", max_tokens=700,
                                         first_text_seconds=2, total_seconds=5, on_text=on_text)
        self.assertEqual((outcome.error, outcome.text), ("provider", "Partial 1240"))

    async def test_stream_failures_are_provider_failures_and_close_the_stream(self):
        failing = _EventStream(_events("Hel")[:2], error=_ClientError("ModelStreamErrorException", "stream broke"))
        with self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs, self.assertRaises(AgentSaffronError):
            [event async for event in _nova(_FakeBedrock(stream=failing)).stream("approved answer")]
        self.assertIn("ModelStreamErrorException: stream broke", logs.output[0])
        self.assertTrue(failing.closed)
        with self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs, self.assertRaises(AgentSaffronError):
            [event async for event in _nova(_FakeBedrock(error=_ClientError("AccessDeniedException", "denied"))).stream("approved answer")]
        self.assertIn("access denied", logs.output[0])
        with self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs, self.assertRaises(AgentSaffronError):
            [event async for event in _nova(_FakeBedrock(stream=_EventStream([{"throttlingException": {"message": "slow down"}}]))).stream("approved answer")]
        self.assertIn("throttlingException", logs.output[0])

    async def test_a_stream_abandoned_by_its_caller_is_closed(self):
        stream = _EventStream(_events("one ", "two ", "three"))
        iterator = _nova(_FakeBedrock(stream=stream)).stream("approved answer").__aiter__()
        self.assertEqual((await iterator.__anext__()).text, "one ")
        await iterator.aclose()
        self.assertTrue(stream.closed)

    async def test_a_stream_that_never_starts_is_abandoned_at_the_deadline(self):
        model = _nova(_FakeBedrock(stream=_EventStream(_events("late")), delay=0.5), timeout_seconds=0.05)
        with self.assertLogs("app.providers.bedrock_converse", "WARNING") as logs, self.assertRaises(AgentSaffronError):
            [event async for event in model.stream("approved answer")]
        self.assertIn("no answer within 0.05s", logs.output[0])


class BedrockRuntimeWiringTests(unittest.TestCase):
    def _settings(self, model_id, **options):
        return AppSettings(environment="test", control_database_url=":memory:", model_provider="bedrock", bedrock_region="ap-south-1",
                           anthropic_model_id=model_id, **options)

    def test_a_nova_id_builds_the_converse_model_for_every_role(self):
        settings = self._settings(NOVA, agent_planner="model", platform_enabled=True, conversation_stream_seconds=20.0)
        settings.ensure_safe_for_production()
        runtime = build_runtime(settings)
        try:
            answers = runtime.assistant.model
            planner = runtime.platform.agent.model_planner.model
            conversation = runtime.dialogue.model
            for model in (answers, planner, conversation, runtime.dialogue.wording_model):
                self.assertIsInstance(model, BedrockConverseModel)
                self.assertEqual((model.model_id, model.aws_region, model.provider_id), (NOVA, "ap-south-1", "bedrock"))
            self.assertEqual((answers.timeout_seconds, conversation.timeout_seconds), (settings.model_timeout_seconds, 20.0))
            # The latency instruction steers Claude's thinking; Nova gets none.
            self.assertIsNone(answers.system)
        finally:
            runtime.close()

    def test_a_claude_id_keeps_the_anthropic_provider(self):
        settings = self._settings(CLAUDE, agent_planner="model")
        answers, conversation, planner = _build_model(settings), _build_conversation_model(settings, None), _build_planner_model(settings, None)
        for model in (answers, conversation, planner):
            self.assertIsInstance(model, AnthropicProvider)
            self.assertEqual((model.platform, model.model_id), ("bedrock", CLAUDE))
        self.assertEqual((answers.system, planner.effort), (LATENCY_SENSITIVE_SYSTEM, "medium"))
        self.assertEqual(conversation.timeout_seconds, settings.conversation_stream_seconds)

    def test_the_conversation_model_id_picks_its_own_adapter(self):
        conversation = _build_conversation_model(self._settings(CLAUDE, conversation_model_id="apac.amazon.nova-lite-v1:0"), None)
        self.assertIsInstance(conversation, BedrockConverseModel)
        self.assertEqual(conversation.model_id, "apac.amazon.nova-lite-v1:0")
        conversation = _build_conversation_model(self._settings(NOVA, conversation_model_id="anthropic.claude-haiku-4-5"), None)
        self.assertIsInstance(conversation, AnthropicProvider)

    def test_the_anthropic_platform_never_uses_converse(self):
        settings = AppSettings(environment="test", model_provider="anthropic", anthropic_model_id="claude-haiku-4-5")
        self.assertIsInstance(_build_model(settings), AnthropicProvider)

    def test_the_open_task_agent_keeps_claude_and_is_off_for_a_non_claude_id(self):
        settings = self._settings(NOVA, open_task_enabled=True, platform_enabled=True)
        settings.ensure_safe_for_production()
        self.assertIsInstance(_build_open_task_model(settings), AnthropicProvider)
        settings = self._settings(NOVA, open_task_enabled=True, platform_enabled=True, open_task_model_id=NOVA)
        with self.assertLogs("app.api.dependencies", "WARNING"):
            self.assertIsNone(_build_open_task_model(settings))

    def test_production_switches_by_the_legacy_model_id_variable_alone(self):
        env = {"GURU_MODEL_PROVIDER": "bedrock", "GURU_ANTHROPIC_MODEL_ID": NOVA, "AWS_REGION": "ap-south-1"}
        adopt_legacy_names(env)
        with patch.dict(os.environ, env, clear=True):
            settings = AppSettings.from_env()
        self.assertEqual((settings.model_provider, settings.anthropic_model_id, settings.bedrock_region), ("bedrock", NOVA, "ap-south-1"))
        settings.ensure_safe_for_production()
        self.assertIsInstance(_build_model(settings), BedrockConverseModel)


if __name__ == "__main__":
    unittest.main()
