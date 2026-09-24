"""Optional LiteLLM SDK adapter.

LiteLLM is kept behind the Agentic Saffron model protocol. Its response objects never
cross the orchestration or HTTP boundary, and importing the adapter does not
make the dependency mandatory for deterministic or Ollama deployments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from ..domain.errors import ErrorCode, AgenticSaffronError, PublicError
from .model_base import ModelEvent, ProviderCapabilities


def _provider_error(message: str) -> AgenticSaffronError:
    return AgenticSaffronError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, message, "provider"))


def _content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, dict):
        message = first.get("message")
    value = getattr(message, "content", None) if message is not None else None
    if value is None and isinstance(message, dict):
        value = message.get("content")
    return value.strip() if isinstance(value, str) else ""


def _chunk_text(chunk: object) -> tuple[str, bool]:
    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")
    if not choices:
        return "", False
    first = choices[0]
    delta = getattr(first, "delta", None)
    if delta is None and isinstance(first, dict):
        delta = first.get("delta")
    value = getattr(delta, "content", None) if delta is not None else None
    if value is None and isinstance(delta, dict):
        value = delta.get("content")
    finish_reason = getattr(first, "finish_reason", None)
    if finish_reason is None and isinstance(first, dict):
        finish_reason = first.get("finish_reason")
    return (value if isinstance(value, str) else ""), bool(finish_reason)


def _usage(chunk: object) -> dict[str, int | float] | None:
    raw = getattr(chunk, "usage", None)
    if raw is None and isinstance(chunk, dict):
        raw = chunk.get("usage")
    if raw is None:
        return None
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
    output: dict[str, int | float] = {}
    for name in fields:
        value = getattr(raw, name, None)
        if value is None and isinstance(raw, dict):
            value = raw.get(name)
        if isinstance(value, (int, float)):
            output[name] = value
    return output or None


@dataclass(frozen=True, slots=True)
class LiteLLMProvider:
    model_id: str
    api_base: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 8.0
    provider_id: str = "litellm"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_json_schema=True,
            supports_tools=True,
            supports_multimodal=True,
            supports_realtime=False,
            cost_metadata="provider_usage_or_gateway_cost",
        )

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("LiteLLM model ID must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("LiteLLM timeout must be positive")

    @staticmethod
    def _module() -> Any:
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise _provider_error("LiteLLM is not installed in this deployment.") from exc
        return litellm

    def _kwargs(self, prompt: str, max_tokens: int, stream: bool) -> dict[str, object]:
        return {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": stream,
            "timeout": self.timeout_seconds,
            **({"api_base": self.api_base} if self.api_base else {}),
            **({"api_key": self.api_key} if self.api_key else {}),
        }

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        if not prompt.strip():
            raise ValueError("model prompt must not be blank")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        module = self._module()
        try:
            response = await module.acompletion(**self._kwargs(prompt, max_tokens, False))
        except Exception as exc:  # provider SDK exception types vary by backend
            raise _provider_error("The configured LiteLLM provider is unavailable.") from exc
        answer = _content(response)
        if not answer:
            raise _provider_error("The configured LiteLLM provider returned no usable answer.")
        return answer

    async def _stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        if not prompt.strip():
            raise ValueError("model prompt must not be blank")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        module = self._module()
        try:
            response = await module.acompletion(**self._kwargs(prompt, max_tokens, True))
            if hasattr(response, "__aiter__"):
                async for chunk in response:
                    text, is_final = _chunk_text(chunk)
                    if text or is_final:
                        yield ModelEvent(
                            type="delta", text=text, is_final=is_final,
                            provider_id=self.provider_id, model_id=self.model_id,
                            usage=_usage(chunk),
                        )
            elif isinstance(response, Iterable):
                for chunk in response:
                    text, is_final = _chunk_text(chunk)
                    if text or is_final:
                        yield ModelEvent(
                            type="delta", text=text, is_final=is_final,
                            provider_id=self.provider_id, model_id=self.model_id,
                            usage=_usage(chunk),
                        )
            else:
                text = _content(response)
                if text:
                    yield ModelEvent(
                        type="delta", text=text, is_final=True,
                        provider_id=self.provider_id, model_id=self.model_id,
                    )
        except AgenticSaffronError:
            raise
        except Exception as exc:
            raise _provider_error("The configured LiteLLM provider is unavailable.") from exc

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        return self._stream(prompt, max_tokens=max_tokens)


__all__ = ["LiteLLMProvider"]
