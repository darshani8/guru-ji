"""Read a model reply as it is written, with deadlines, for speech that starts early.

Any ``TextModel`` works: Claude and LiteLLM stream token by token, Ollama
yields its whole answer at once. Two deadlines apply: the first text must
arrive within ``first_text_seconds``, and the whole reply within
``total_seconds``. The opening is held back until it is clear it is not the
``[[search: ...]]`` marker, which is never spoken. Provider failures (an
unreachable model, a refusal of the whole fallback chain) are returned, not
raised; cancellation still propagates, so talking over Agent Saffron stops the
generation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from ..domain.errors import AgentSaffronError
from .prompts import split_search_request

logger = logging.getLogger("saffron.conversation.stream")

MAX_MARKER_CHARS = 320


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    text: str = ""
    emitted_any: bool = False
    search: str | None = None
    error: str | None = None  # timeout_first | timeout_total | provider

    @property
    def ok(self) -> bool:
        return self.error is None


async def stream_reply(
    model: Any,
    prompt: str,
    *,
    max_tokens: int,
    first_text_seconds: float,
    total_seconds: float,
    on_text: Callable[[str], Awaitable[None]],
    detect_search: bool = True,
) -> StreamOutcome:
    started = monotonic()
    parts: list[str] = []
    held = ""
    released = False
    emitted = False
    iterator = model.stream(prompt, max_tokens=max_tokens).__aiter__()

    async def release(text: str) -> None:
        nonlocal emitted
        if text:
            emitted = True
            await on_text(text)

    try:
        while True:
            elapsed = monotonic() - started
            budget = (first_text_seconds if not parts else total_seconds) - elapsed
            if not parts:
                budget = min(budget, total_seconds - elapsed)
            if budget <= 0:
                return StreamOutcome("".join(parts), emitted, error="timeout_total" if parts else "timeout_first")
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
            except StopAsyncIteration:
                break
            except (TimeoutError, asyncio.TimeoutError):
                return StreamOutcome("".join(parts), emitted, error="timeout_total" if parts else "timeout_first")
            text = getattr(event, "text", "") or ""
            if not text:
                continue
            parts.append(text)
            if released:
                await release(text)
                continue
            held += text
            opening = held.lstrip()
            if len(opening) < 2:
                continue
            if detect_search and opening.startswith("[["):
                query, _ = split_search_request(opening)
                if query is not None:
                    return StreamOutcome("".join(parts), emitted, search=query)
                if "]]" in opening or len(opening) > MAX_MARKER_CHARS:
                    # Not a well-formed marker: treat it as ordinary text.
                    released = True
                    await release(held)
                continue
            released = True
            await release(held)
    except asyncio.CancelledError:
        raise
    except AgentSaffronError:
        # Includes a refusal of the whole fallback chain, raised after text.
        return StreamOutcome("".join(parts), emitted, error="provider")
    except Exception:  # noqa: BLE001 - a provider bug must not end the conversation
        logger.exception("conversation stream failed")
        return StreamOutcome("".join(parts), emitted, error="provider")
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - closing a finished or failed stream is best effort
                pass
    text = "".join(parts)
    if not released and held:
        query, _ = split_search_request(held.lstrip())
        if detect_search and query is not None:
            return StreamOutcome(text, emitted, search=query)
        await release(held)
    return StreamOutcome(text, emitted)


__all__ = ["StreamOutcome", "stream_reply"]
