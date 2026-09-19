"""Treat web text as data, never as executable instructions."""

from dataclasses import dataclass

INJECTION_MARKERS = ("ignore previous instructions", "system message", "reveal your prompt", "send credentials")


@dataclass(frozen=True, slots=True)
class UntrustedContent:
    url: str
    text: str
    warnings: tuple[str, ...] = ()


def wrap_untrusted(url: str, text: str) -> UntrustedContent:
    normalized = text.lower()
    warnings = tuple("possible_instruction_smuggling" for marker in INJECTION_MARKERS if marker in normalized)
    return UntrustedContent(url=url, text=text, warnings=warnings)


def safe_excerpt(content: UntrustedContent, limit: int = 2000) -> str:
    if limit <= 0:
        raise ValueError("limit must be positive")
    return content.text[:limit]


__all__ = ["UntrustedContent", "safe_excerpt", "wrap_untrusted"]
