"""Optional local Ollama adapter with an explicit network boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..domain.errors import ErrorCode, AgenticSaffronError, PublicError
from .model_base import ModelEvent, ProviderCapabilities


def _provider_error(message: str) -> AgenticSaffronError:
    return AgenticSaffronError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, message, "provider"))


@dataclass(frozen=True, slots=True)
class OllamaProvider:
    base_url: str | None = None
    model_id: str = "llama3.1:8b"
    timeout_seconds: float = 8.0
    transport: httpx.AsyncBaseTransport | None = None
    provider_id: str = "ollama"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_json_schema=False,
            supports_tools=False,
            supports_multimodal=False,
            supports_realtime=False,
        )

    def __post_init__(self) -> None:
        if not self.base_url:
            return
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Ollama base URL must be an absolute HTTP(S) URL")
        if self.timeout_seconds <= 0:
            raise ValueError("Ollama timeout must be positive")
        if not self.model_id.strip():
            raise ValueError("Ollama model ID must not be blank")

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        if not self.base_url:
            raise _provider_error("Local model provider is not configured.")
        if not prompt.strip():
            raise ValueError("model prompt must not be blank")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        payload = {
            "model": self.model_id,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post("/api/generate", json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _provider_error("The configured local model provider is unavailable.") from exc
        answer = data.get("response") if isinstance(data, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            raise _provider_error("The configured local model provider returned no usable answer.")
        return answer.strip()

    async def _stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        answer = await self.complete(prompt, max_tokens=max_tokens)
        yield ModelEvent(
            type="delta",
            text=answer,
            is_final=True,
            provider_id=self.provider_id,
            model_id=self.model_id,
            usage=None,
        )

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        return self._stream(prompt, max_tokens=max_tokens)


__all__ = ["OllamaProvider"]
