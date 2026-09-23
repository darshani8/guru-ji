"""Claude adapter over the official Anthropic SDK.

Claude is reached either on the Claude API or in Amazon Bedrock, whose
Messages endpoint takes the same request body. The SDK is an optional extra
(``anthropic``); importing this module does not require it, so deterministic,
Ollama and LiteLLM deployments are unaffected. Provider failures surface as a
generic public error so callers fall back to their deterministic path; the
specific reason is logged for the operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..domain.errors import ErrorCode, GuruJiError, PublicError
from .model_base import ModelEvent, ProviderCapabilities

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PLATFORMS = ("anthropic", "bedrock")
# Claude Opus 5 thinks by default and max_tokens caps thinking plus answer, so
# the small caps callers size for the answer alone would cut replies short.
# Output length is governed by the prompts, not by this ceiling.
MIN_MAX_TOKENS = 16_000
# A declined request is re-run server-side on Anthropic's recommended model for
# that refusal category. Only models documented with the "default" mode get it.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
# Bedrock has no server-side fallbacks, so the SDK middleware retries a declined
# request there instead, on the model the "default" mode sends cyber refusals to.
BEDROCK_FALLBACK_MODEL = "anthropic.claude-opus-4-8"
# Models that reject output_config.effort; they run at their single default.
NO_EFFORT_MODELS = ("claude-haiku-4-5",)
# Cuts the thinking Claude does before its first visible text on routes a
# person is waiting on (voice and chat answers).
LATENCY_SENSITIVE_SYSTEM = "Latency-sensitive; begin your visible answer immediately."

_PUBLIC_MESSAGE = "The configured Claude model is unavailable."


def _base_model(model_id: str) -> str:
    """The Claude API name behind a Bedrock ID such as global.anthropic.claude-opus-5."""

    return model_id.split("anthropic.")[-1]


def _provider_error() -> GuruJiError:
    return GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, _PUBLIC_MESSAGE, "provider"))


def _describe_failure(exc: Exception) -> str:
    """Name an SDK failure for the operator log; callers only see the public message."""

    try:
        import anthropic
    except ImportError:  # pragma: no cover - the client could not have been built
        return type(exc).__name__
    if isinstance(exc, anthropic.AuthenticationError):
        return f"authentication failed: {exc.message[:300]}"
    # On Bedrock this is also how an account without access to the model is told.
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"access denied: {exc.message[:300]}"
    if isinstance(exc, anthropic.NotFoundError):
        return f"model not found: {exc.message[:300]}"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited"
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code}: {exc.message[:300]}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "the API could not be reached"
    # For example "Could not resolve AWS credentials from session" on Bedrock.
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _usage(message: Any) -> dict[str, int | float] | None:
    usage = getattr(message, "usage", None)
    output = {
        name: value
        for name in ("input_tokens", "output_tokens", "cache_read_input_tokens")
        if isinstance(value := getattr(usage, name, None), (int, float))
    }
    return output or None


@dataclass(slots=True)
class AnthropicProvider:
    model_id: str = DEFAULT_MODEL_ID
    api_key: str | None = field(default=None, repr=False)
    effort: str = "low"
    system: str | None = None
    timeout_seconds: float = 8.0
    platform: str = "anthropic"
    # Bedrock only; None lets the SDK read AWS_REGION.
    aws_region: str | None = None
    # Injected in tests; built from the SDK on first use otherwise.
    client: Any = field(default=None, repr=False, compare=False)
    transport: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("Claude model ID must not be blank")
        if self.platform not in PLATFORMS:
            raise ValueError(f"Claude platform must be one of {', '.join(PLATFORMS)}")
        if self.platform == "bedrock" and self.model_id.startswith("claude-"):
            # Bedrock rejects the Claude API names; its IDs carry a provider prefix.
            self.model_id = f"anthropic.{self.model_id}"
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"Claude effort must be one of {', '.join(EFFORT_LEVELS)}")
        if self.timeout_seconds <= 0:
            raise ValueError("Claude timeout must be positive")

    @property
    def provider_id(self) -> str:
        return self.platform

    @property
    def _falls_back(self) -> bool:
        return _base_model(self.model_id) in FALLBACK_MODELS

    @property
    def _takes_effort(self) -> bool:
        return not _base_model(self.model_id).startswith(NO_EFFORT_MODELS)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_json_schema=True,
            supports_tools=True,
            supports_multimodal=True,
            supports_realtime=False,
            cost_metadata="anthropic_usage",
        )

    def _messages(self) -> Any:
        if self.client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise self._unavailable("the anthropic SDK is not installed (install the 'anthropic' extra)") from exc
            http_client = anthropic.DefaultAsyncHttpxClient(transport=self.transport) if self.transport else None
            try:
                if self.platform == "bedrock":
                    middleware = [anthropic.BetaRefusalFallbackMiddleware([{"model": BEDROCK_FALLBACK_MODEL}])] if self._falls_back else None
                    # Signed with the AWS credential chain (such as the ECS task
                    # role), or AWS_BEARER_TOKEN_BEDROCK when that is set.
                    self.client = anthropic.AsyncAnthropicBedrockMantle(
                        aws_region=self.aws_region, timeout=self.timeout_seconds, http_client=http_client, middleware=middleware,
                    )
                else:
                    # api_key=None lets the SDK read ANTHROPIC_API_KEY or another configured credential.
                    self.client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.timeout_seconds, http_client=http_client)
            except Exception as exc:  # noqa: BLE001 - for example no AWS region could be resolved
                raise self._unavailable(f"the client could not be created: {exc}") from exc
        return self.client.beta.messages

    def _fallback_scope(self) -> contextlib.AbstractContextManager[Any]:
        if self.platform != "bedrock" or not self._falls_back:
            return contextlib.nullcontext()
        import anthropic

        # Each call is a self-contained turn, so a fresh state pins nothing across calls.
        return anthropic.BetaFallbackState()

    def _request(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("model prompt must not be blank")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        request: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._takes_effort:
            request["output_config"] = {"effort": self.effort}
        if self.system:
            request["system"] = self.system
        if self.platform == "anthropic" and self._falls_back:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def _unavailable(self, reason: str) -> GuruJiError:
        logger.warning("Claude model %s unavailable: %s", self.model_id, reason)
        return _provider_error()

    def _check_stop(self, message: Any) -> None:
        # A refusal (after any fallback) or a max_tokens cut leaves no complete
        # answer, so neither is ever passed on as one.
        if message.stop_reason == "refusal":
            raise self._unavailable("the request was declined")
        if message.stop_reason == "max_tokens":
            raise self._unavailable("the answer was cut off at max_tokens")

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        request = self._request(prompt, max_tokens)
        messages = self._messages()
        try:
            # One deadline covers the SDK's own retries, so a slow model makes
            # the caller fall back instead of keeping a voice user waiting.
            async with asyncio.timeout(self.timeout_seconds):
                with self._fallback_scope():
                    message = await messages.create(**request)
        except TimeoutError as exc:
            raise self._unavailable(f"no answer within {self.timeout_seconds:g}s") from exc
        except Exception as exc:  # noqa: BLE001 - classified by _describe_failure
            raise self._unavailable(_describe_failure(exc)) from exc
        self._check_stop(message)
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise self._unavailable("the answer contained no text")
        return text

    async def _stream(self, prompt: str, *, max_tokens: int) -> AsyncIterator[ModelEvent]:
        request = self._request(prompt, max_tokens)
        messages = self._messages()
        try:
            with self._fallback_scope():
                async with messages.stream(**request) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield ModelEvent(type="delta", text=text, provider_id=self.provider_id, model_id=self.model_id)
                    message = await stream.get_final_message()
        except GuruJiError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by _describe_failure
            raise self._unavailable(_describe_failure(exc)) from exc
        self._check_stop(message)
        yield ModelEvent(type="delta", is_final=True, provider_id=self.provider_id, model_id=self.model_id, usage=_usage(message))

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        return self._stream(prompt, max_tokens=max_tokens)


__all__ = [
    "BEDROCK_FALLBACK_MODEL",
    "DEFAULT_MODEL_ID",
    "EFFORT_LEVELS",
    "FALLBACK_BETA",
    "LATENCY_SENSITIVE_SYSTEM",
    "MIN_MAX_TOKENS",
    "PLATFORMS",
    "AnthropicProvider",
]
