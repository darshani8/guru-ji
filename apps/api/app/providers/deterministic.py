"""No-network provider used by the local prototype."""

class DeterministicProvider:
    provider_id = "deterministic-demo"

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        del max_tokens
        return prompt.strip()


__all__ = ["DeterministicProvider"]
