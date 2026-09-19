import json
import unittest

import httpx

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings
from app.domain.errors import ErrorCode, GuruJiError, PublicError
from app.orchestration.answer_synthesizer import AssistantAnswer, apply_model_wording
from app.providers.ollama import OllamaProvider


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


if __name__ == "__main__":
    unittest.main()
