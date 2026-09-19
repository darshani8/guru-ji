from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

BASE_URL = os.getenv("GURU_BASE_URL", "http://127.0.0.1:8000")
HEADERS = {
    "Authorization": "Bearer dev-token",
    "X-Demo-Principal": "smoke-script",
    "X-Demo-Role": "main_admin",
    "X-Demo-College": "college_a",
    "X-Demo-Capabilities": "ask:read_only,source:view_metadata,access:manage",
}


def call(path: str, method: str = "GET", payload: dict[str, object] | None = None) -> dict[str, object]:
    headers = dict(HEADERS)
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(BASE_URL + path, method=method, headers=headers, data=body)
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"{method} {path} returned {response.status}")
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    ready = call("/v1/health/ready")
    assert ready["status"] == "ready" and ready["database_ok"] is True
    sources = call("/v1/sources")
    assert sources["sources"]
    answer = call("/v1/chat", "POST", {"prompt": "Give me the institutional overview", "institution_scope": {"college_id": "college_a"}, "channel": "text"})
    assert answer["status"] == "complete" and answer["citations"]
    briefing = call("/v1/briefings/daily", "POST", {"college_id": "college_a"})
    assert briefing["briefing_id"]
    recent = call("/v1/audit/recent?limit=10")
    assert recent["events"]
    print("LIVE_SMOKE_OK")


if __name__ == "__main__":
    main()
