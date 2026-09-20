"""Allowlisted MCP/agent-gateway boundary.

The API never forwards arbitrary client-provided tool URLs. Targets and tools
are deployment configuration, and each target is still called only after the
normal capability/scope policy decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..domain.principals import Capability


@dataclass(frozen=True, slots=True)
class GatewayTool:
    target_id: str
    tool_name: str
    required_capability: Capability


@dataclass(frozen=True, slots=True)
class GatewayTarget:
    target_id: str
    base_url: str
    auth_token: str
    tools: tuple[GatewayTool, ...]

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("gateway target URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("gateway target URL must not contain credentials, query, or fragment data")
        if not self.target_id.strip() or not self.auth_token.strip():
            raise ValueError("gateway target ID and auth token are required")
        if any(item.target_id != self.target_id for item in self.tools):
            raise ValueError("gateway tool target IDs must match the gateway target")


class McpGatewayAllowlist:
    def __init__(self, targets: tuple[GatewayTarget, ...] = ()) -> None:
        self._targets = {target.target_id: target for target in targets}
        if len(self._targets) != len(targets):
            raise ValueError("gateway target IDs must be unique")

    def resolve(self, target_id: str, tool_name: str) -> tuple[GatewayTarget, GatewayTool]:
        target = self._targets.get(target_id)
        if target is None:
            raise PermissionError("gateway target is not allowlisted")
        for tool in target.tools:
            if tool.tool_name == tool_name:
                return target, tool
        raise PermissionError("gateway tool is not allowlisted")

    def all_targets(self) -> tuple[GatewayTarget, ...]:
        return tuple(self._targets.values())


__all__ = ["GatewayTarget", "GatewayTool", "McpGatewayAllowlist"]
