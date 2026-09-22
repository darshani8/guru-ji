"""Optional OpenFGA relationship checker with fail-closed semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True, slots=True)
class OpenFgaRelationshipChecker:
    base_url: str
    store_id: str
    authorization_model_id: str
    timeout_seconds: float = 1.5
    transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OpenFGA URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OpenFGA URL must not contain credentials, query, or fragment data")
        if not self.store_id.strip() or not self.authorization_model_id.strip():
            raise ValueError("OpenFGA store and authorization model IDs are required")
        if self.timeout_seconds <= 0:
            raise ValueError("OpenFGA timeout must be positive")

    def check(self, *, user: str, relation: str, object_id: str) -> bool:
        if not user.strip() or not relation.strip() or not object_id.strip():
            return False
        payload = {
            "tuple_key": {"user": user, "relation": relation, "object": object_id},
            "authorization_model_id": self.authorization_model_id,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport, follow_redirects=False) as client:
                response = client.post(
                    f"{self.base_url.rstrip('/')}/stores/{self.store_id}/check",
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                value = response.json()
            return isinstance(value, dict) and value.get("allowed") is True
        except (httpx.HTTPError, ValueError, TypeError):
            return False


__all__ = ["OpenFgaRelationshipChecker"]
