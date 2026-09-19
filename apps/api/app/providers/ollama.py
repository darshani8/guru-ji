"""Optional local Ollama adapter; disabled unless explicitly configured."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.errors import ErrorCode, GuruJiError, PublicError


@dataclass(frozen=True, slots=True)
class OllamaProvider:
    base_url: str | None = None
    model_id: str = "llama3.1:8b"
    provider_id: str = "ollama"

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        del prompt, max_tokens
        if not self.base_url:
            raise GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "Local model provider is not configured.", "provider"))
        raise GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "Network model calls are intentionally disabled in the local reference build.", "provider"))


__all__ = ["OllamaProvider"]
