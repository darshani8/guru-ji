"""Read-only HTTP connector for institution-local semantic tool services.

The application talks to a connector service, never to an institution's raw
database. The remote service contract is intentionally semantic: health is
``GET /v1/health`` and approved tools are ``POST /v1/execute``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from ..domain.provenance import Provenance, SourceKind, Warning
from ..domain.results import ResultStatus, ToolResult
from ..domain.source_health import Freshness, SourceHealth, SourceHealthStatus
from ..policy.query_limits import QueryLimits
from .base import ConnectorContext, unavailable_result


class _ResponseTooLarge(ValueError):
    pass


async def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise _ResponseTooLarge("connector response exceeded the configured bound")
        except ValueError as exc:
            raise ValueError("connector returned an invalid content length") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise _ResponseTooLarge("connector response exceeded the configured bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _safe_text(value: object, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value.strip()[:500]


@dataclass(frozen=True, slots=True)
class RemoteHttpConnector:
    """Adapter for a reviewed institution-local semantic connector service."""

    source_id: str
    institution_id: str
    display_name: str
    base_url: str
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 5.0
    max_response_bytes: int = 1_000_000
    auth_token: str | None = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in ("source_id", "institution_id", "display_name", "base_url"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("connector base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("connector base URL must not contain credentials, query, or fragment data")
        if not self.allowed_tools:
            raise ValueError("connector must declare at least one approved tool")
        if self.timeout_seconds <= 0:
            raise ValueError("connector timeout must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("connector max response bytes must be positive")
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))

    @property
    def _root(self) -> str:
        return self.base_url.rstrip("/")

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if request_id:
            headers["X-Request-ID"] = request_id
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        request_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
            async with client.stream(
                method,
                f"{self._root}{path}",
                headers={**self._headers(request_id), **({"Content-Type": "application/json"} if payload is not None else {})},
                json=payload,
            ) as response:
                response.raise_for_status()
                raw = await _read_bounded(response, self.max_response_bytes)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("connector returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("connector response must be a JSON object")
        return value

    async def health(self) -> SourceHealth:
        checked_at = datetime.now(timezone.utc)
        started = checked_at
        try:
            data = await self._request_json("GET", "/v1/health")
        except httpx.TimeoutException:
            return SourceHealth(
                source_id=self.source_id,
                institution_id=self.institution_id,
                status=SourceHealthStatus.UNAVAILABLE,
                checked_at=checked_at,
                display_name=self.display_name,
                connector_type="remote_http",
                freshness=Freshness.UNKNOWN,
                detail="remote connector health request timed out",
            )
        except (_ResponseTooLarge, httpx.HTTPError, ValueError):
            return SourceHealth(
                source_id=self.source_id,
                institution_id=self.institution_id,
                status=SourceHealthStatus.UNAVAILABLE,
                checked_at=checked_at,
                display_name=self.display_name,
                connector_type="remote_http",
                freshness=Freshness.UNKNOWN,
                detail="remote connector health response was unavailable or invalid",
            )
        latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        try:
            status = SourceHealthStatus(str(data.get("status", "unknown")))
        except ValueError:
            status = SourceHealthStatus.UNKNOWN
        try:
            freshness = Freshness(str(data.get("freshness", "unknown")))
        except ValueError:
            freshness = Freshness.UNKNOWN
        return SourceHealth(
            source_id=self.source_id,
            institution_id=self.institution_id,
            status=status,
            checked_at=checked_at,
            display_name=self.display_name,
            connector_type="remote_http",
            last_success_at=_parse_datetime(data.get("last_success_at")),
            latency_ms=latency_ms,
            freshness=freshness,
            detail=_safe_text(data.get("detail"), "remote connector health reported"),
        )

    @staticmethod
    def _warning(value: object, source_id: str) -> Warning | None:
        if not isinstance(value, dict):
            return None
        code = _safe_text(value.get("code"))
        message = _safe_text(value.get("message"))
        if not code or not message:
            return None
        return Warning(code=code, message=message, source_id=_safe_text(value.get("source_id"), source_id) or source_id)

    def _parse_result(self, tool_name: str, data: dict[str, Any]) -> ToolResult:
        if _safe_text(data.get("tool_name"), tool_name) != tool_name:
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "remote result tool name did not match the request", self.source_id),
            ))
        try:
            status = ResultStatus(_safe_text(data.get("status")))
        except ValueError:
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "remote result contained an unknown status", self.source_id),
            ))
        raw_warnings = data.get("warnings", [])
        if not isinstance(raw_warnings, list):
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "remote result warnings were not a list", self.source_id),
            ))
        warnings = tuple(item for raw in raw_warnings if (item := self._warning(raw, self.source_id)) is not None)
        raw_provenance = data.get("provenance", [])
        if not isinstance(raw_provenance, list):
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "remote result provenance was not a list", self.source_id),
            ))
        provenance: list[Provenance] = []
        try:
            for item in raw_provenance:
                if not isinstance(item, dict) or _safe_text(item.get("source_id")) != self.source_id:
                    raise ValueError("provenance source did not match connector source")
                provenance.append(Provenance(
                    source_id=self.source_id,
                    source_type=SourceKind(_safe_text(item.get("source_type"))),
                    retrieved_at=_parse_datetime(item.get("retrieved_at")) or datetime.now(timezone.utc),
                    complete=bool(item.get("complete", True)),
                    rows_used=int(item.get("rows_used", 0)),
                    redactions_applied=tuple(str(value) for value in item.get("redactions_applied", [])),
                ))
        except (TypeError, ValueError):
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "remote result provenance was invalid", self.source_id),
            ))
        if status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL, ResultStatus.STALE} and not provenance:
            return ToolResult(tool_name=tool_name, status=ResultStatus.INVALID_RESULT, warnings=(
                Warning("invalid_remote_result", "successful remote results must include provenance", self.source_id),
            ))
        return ToolResult(
            tool_name=tool_name,
            status=status,
            data=data.get("data"),
            provenance=tuple(provenance),
            warnings=warnings,
        )

    async def execute(self, tool_name: str, arguments: dict[str, object], context: ConnectorContext) -> ToolResult:
        if tool_name not in self.allowed_tools:
            return unavailable_result(tool_name, self.source_id, "tool is not enabled by the connector contract")
        payload = {
            "source_id": self.source_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "request_id": context.request_id,
            "limits": {
                "max_duration_ms": context.limits.max_duration_ms,
                "max_rows": context.limits.max_rows,
                "max_response_bytes": min(context.limits.max_response_bytes, self.max_response_bytes),
            },
        }
        try:
            data = await self._request_json("POST", "/v1/execute", request_id=context.request_id, payload=payload)
            return self._parse_result(tool_name, data)
        except httpx.TimeoutException:
            return ToolResult(tool_name=tool_name, status=ResultStatus.TIMEOUT, warnings=(
                Warning("source_timeout", "remote connector request timed out", self.source_id),
            ))
        except (_ResponseTooLarge, httpx.HTTPError, ValueError):
            return ToolResult(tool_name=tool_name, status=ResultStatus.UNAVAILABLE, warnings=(
                Warning("source_unavailable", "remote connector response was unavailable or invalid", self.source_id),
            ))


__all__ = ["RemoteHttpConnector"]
