"""Non-Claude models in Amazon Bedrock, such as Amazon Nova, over the Converse API.

Claude in Bedrock goes through the Anthropic SDK (``anthropic.py``). Any other
Bedrock model ID (``apac.amazon.nova-pro-v1:0``) is answered here by boto3's
``bedrock-runtime`` Converse and ConverseStream calls, so with
SAFFRON_MODEL_PROVIDER=bedrock the model ID alone picks the adapter. boto3 is
the optional ``aws`` extra and blocks, so every call runs on a worker thread,
never on the event loop. Failures surface as the same generic public error the
Claude adapter raises, so callers fall back to their deterministic path; the
specific reason is logged for the operator.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..domain.errors import ErrorCode, AgenticSaffronError, PublicError
from .model_base import ModelEvent, ProviderCapabilities

logger = logging.getLogger(__name__)

# Callers size max_tokens for the answer alone; the headroom keeps a slightly
# long reply from being cut off and discarded. Every Nova text model (and most
# other Bedrock text models) accepts at least this many output tokens.
MIN_MAX_TOKENS = 2_048
# Stop reasons that mean the answer is complete.
_COMPLETE = frozenset({"end_turn", "stop_sequence"})
_MAX_WORKERS = 32
_PUBLIC_MESSAGE = "The configured Bedrock model is unavailable."

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def is_claude_model(model_id: str) -> bool:
    """Whether a model ID names Claude, which the Anthropic SDK serves.

    Claude API names (``claude-haiku-4-5``), Bedrock IDs and inference profiles
    (``global.anthropic.claude-haiku-4-5-20251001-v1:0``) and ARNs naming one
    are Claude. An application inference profile ARN names no model, so it keeps
    the Claude path it always had. Everything else (``apac.amazon.nova-pro-v1:0``)
    is not.
    """

    model = model_id.strip().lower()
    if model.startswith("claude") or "anthropic." in model:
        return True
    return model.startswith("arn:") and ":application-inference-profile/" in model


def _pool() -> ThreadPoolExecutor:
    # One pool for every instance, sized for concurrent voice turns, and apart
    # from the loop's default executor so a slow model never starves other work.
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="bedrock-converse")
        return _executor


def _provider_error() -> AgenticSaffronError:
    return AgenticSaffronError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, _PUBLIC_MESSAGE, "provider"))


def _describe_failure(exc: BaseException) -> str:
    """Name a boto3 failure for the operator log; callers only see the public message."""

    error = getattr(exc, "response", None)
    error = error.get("Error", {}) if isinstance(error, dict) else {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or exc)[:300]
    if code == "AccessDeniedException":
        # Also how an account without model access, or with a billing problem, is told.
        return f"access denied: {message}"
    if code == "ResourceNotFoundException":
        return f"model not found: {message}"
    if code in {"ThrottlingException", "ServiceQuotaExceededException"}:
        return f"rate limited: {message}"
    if code == "ValidationException":
        return f"request rejected: {message}"
    if code == "ModelTimeoutException":
        return "timed out"
    if code:
        return f"{code}: {message}"
    name = type(exc).__name__
    if name in {"ReadTimeoutError", "ConnectTimeoutError"}:
        return "timed out"
    if name in {"EndpointConnectionError", "ConnectionClosedError"}:
        return "the API could not be reached"
    # For example NoCredentialsError or NoRegionError.
    return f"{name}: {str(exc)[:300]}"


def _usage(raw: Any) -> dict[str, int | float] | None:
    if not isinstance(raw, dict):
        return None
    names = {"inputTokens": "input_tokens", "outputTokens": "output_tokens", "cacheReadInputTokens": "cache_read_input_tokens"}
    output = {name: value for key, name in names.items() if isinstance(value := raw.get(key), (int, float))}
    return output or None


def _text(response: dict[str, Any]) -> str:
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    return "".join(block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)).strip()


@dataclass(slots=True)
class BedrockConverseModel:
    """A ``TextModel`` over Bedrock Converse: ``complete`` and token-by-token ``stream``."""

    model_id: str
    system: str | None = None
    timeout_seconds: float = 8.0
    # None lets boto3 read AWS_REGION / AWS_DEFAULT_REGION.
    aws_region: str | None = None
    # Injected in tests; built from boto3 on first use otherwise.
    client: Any = field(default=None, repr=False, compare=False)
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    provider_id = "bedrock"
    platform = "bedrock"

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("Bedrock model ID must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("Bedrock timeout must be positive")

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_streaming=True, cost_metadata="bedrock_usage")

    def _unavailable(self, reason: str) -> AgenticSaffronError:
        logger.warning("Bedrock model %s unavailable: %s", self.model_id, reason)
        return _provider_error()

    def _runtime(self) -> Any:
        """The bedrock-runtime client; built on a worker thread, once."""

        with self._client_lock:
            if self.client is None:
                try:
                    import boto3
                    from botocore.config import Config
                except ImportError as exc:
                    raise self._unavailable("boto3 is not installed (install the 'aws' extra)") from exc
                config = Config(
                    connect_timeout=min(self.timeout_seconds, 5.0), read_timeout=self.timeout_seconds,
                    retries={"max_attempts": 2, "mode": "standard"}, max_pool_connections=_MAX_WORKERS,
                )
                try:
                    # A session per client: boto3's default session is not thread safe.
                    self.client = boto3.session.Session().client("bedrock-runtime", region_name=self.aws_region, config=config)
                except Exception as exc:  # noqa: BLE001 - for example no AWS region could be resolved
                    raise self._unavailable(f"the client could not be created: {_describe_failure(exc)}") from exc
            return self.client

    async def _off_loop(self, call: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(_pool(), call, *args)

    def _request(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("model prompt must not be blank")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max(max_tokens, MIN_MAX_TOKENS)},
        }
        if self.system:
            request["system"] = [{"text": self.system}]
        return request

    def _check_stop(self, stop_reason: Any) -> None:
        # A cut-off, filtered or blocked answer is never passed on as a complete one.
        if stop_reason is not None and stop_reason not in _COMPLETE:
            raise self._unavailable(f"the answer stopped with {stop_reason}")

    def _converse(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._runtime().converse(**request)

    def _converse_stream(self, request: dict[str, Any]) -> Any:
        return self._runtime().converse_stream(**request)["stream"]

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        request = self._request(prompt, max_tokens)
        try:
            # One deadline covers boto3's own retries, so a slow model makes the
            # caller fall back instead of keeping a voice user waiting.
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._off_loop(self._converse, request)
        except TimeoutError as exc:
            raise self._unavailable(f"no answer within {self.timeout_seconds:g}s") from exc
        except AgenticSaffronError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by _describe_failure
            raise self._unavailable(_describe_failure(exc)) from exc
        self._check_stop(response.get("stopReason"))
        text = _text(response)
        if not text:
            raise self._unavailable("the answer contained no text")
        return text

    async def _stream(self, prompt: str, *, max_tokens: int) -> AsyncIterator[ModelEvent]:
        request = self._request(prompt, max_tokens)
        events: Any = None
        stop_reason: Any = None
        usage: dict[str, int | float] | None = None
        try:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    events = await self._off_loop(self._converse_stream, request)
            except TimeoutError as exc:
                raise self._unavailable(f"no answer within {self.timeout_seconds:g}s") from exc
            iterator = iter(events)
            done = object()
            while (event := await self._off_loop(next, iterator, done)) is not done:
                if "contentBlockDelta" in event:
                    text = (event["contentBlockDelta"].get("delta") or {}).get("text")
                    if text:
                        yield ModelEvent(type="delta", text=text, provider_id=self.provider_id, model_id=self.model_id)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = _usage(event["metadata"].get("usage"))
                elif any(key.endswith("Exception") for key in event):
                    # botocore raises these itself; a raw error event is handled the same way.
                    raise self._unavailable(f"the stream failed: {', '.join(event)}")
        except AgenticSaffronError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by _describe_failure
            raise self._unavailable(_describe_failure(exc)) from exc
        finally:
            # Stops the HTTP response when the caller gives up (a deadline, the person talking over).
            close = getattr(events, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing a finished or failed stream is best effort
                    pass
        self._check_stop(stop_reason)
        yield ModelEvent(type="delta", is_final=True, provider_id=self.provider_id, model_id=self.model_id, usage=usage)

    def stream(self, prompt: str, *, max_tokens: int = 800) -> AsyncIterator[ModelEvent]:
        return self._stream(prompt, max_tokens=max_tokens)


__all__ = ["MIN_MAX_TOKENS", "BedrockConverseModel", "is_claude_model"]
