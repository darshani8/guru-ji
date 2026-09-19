"""Small bounded HTTP helpers for untrusted public-web responses."""

from __future__ import annotations

import httpx


class WebPayloadTooLarge(ValueError):
    """Raised when a public-web response exceeds the configured byte bound."""


async def read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise WebPayloadTooLarge("public-web response exceeded the configured bound")
        except ValueError as exc:
            if isinstance(exc, WebPayloadTooLarge):
                raise
            raise ValueError("public-web response returned an invalid content length") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise WebPayloadTooLarge("public-web response exceeded the configured bound")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["WebPayloadTooLarge", "read_bounded"]
