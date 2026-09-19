"""Provider protocol; providers receive already-authorized context."""

from typing import Protocol


class TextModel(Protocol):
    provider_id: str

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str: ...


__all__ = ["TextModel"]
