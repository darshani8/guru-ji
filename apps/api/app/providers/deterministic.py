"""No-network provider used by the local prototype."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .model_base import ModelEvent, ProviderCapabilities


class DeterministicProvider:
    provider_id = "deterministic-demo"
    model_id = "deterministic-v1"
    capabilities = ProviderCapabilities(
        supports_streaming=True,
        supports_json_schema=False,
        supports_tools=False,
        supports_multimodal=False,
        supports_realtime=False,
    )

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        del max_tokens
        return prompt.strip()

    async def _stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        del max_tokens
        yield ModelEvent(
            type="delta",
            text=prompt.strip(),
            is_final=True,
            provider_id=self.provider_id,
            model_id=self.model_id,
        )

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        return self._stream(prompt, max_tokens=max_tokens)


__all__ = ["DeterministicProvider"]
