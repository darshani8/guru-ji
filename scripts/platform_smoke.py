"""End-to-end smoke test of the data platform against a running API.

Uploads a small student sheet, waits for the import, asks the agent a
question, generates a report, and downloads it. Uses the development demo
identity, so it only works with GURU_ENVIRONMENT=development.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.request import Request, urlopen

BASE_URL = os.getenv("GURU_BASE_URL", "http://127.0.0.1:8000")
HEADERS = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "platform-smoke", "X-Demo-Role": "principal", "X-Demo-College": "college_a"}


def call(path: str, method: str = "GET", payload: dict[str, object] | None = None, *, raw: bytes | None = None, content_type: str | None = None, role: str | None = None):
    headers = dict(HEADERS)
    if role:
        headers["X-Demo-Role"] = role
    body = raw
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    elif content_type:
        headers["Content-Type"] = content_type
    request = Request(BASE_URL + path, method=method, headers=headers, data=body)
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"{method} {path} returned {response.status}")
        data = response.read()
        if "application/json" in response.headers.get("content-type", ""):
            return json.loads(data.decode("utf-8"))
        return data


def multipart(fields: dict[str, str], file_name: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----guru{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for key, value in fields.items():
        lines.extend([f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{key}"'.encode(), b"", value.encode()])
    lines.extend([f"--{boundary}".encode(), f'Content-Disposition: form-data; name="file"; filename="{file_name}"'.encode(), b"Content-Type: text/csv", b"", content, f"--{boundary}--".encode(), b""])
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    # Registering an institution needs access:manage; the rest of the flow runs as a principal.
    call("/v1/institutions/college_a", "PUT", {"name": "College A", "location": "Bengaluru", "email_domains": ["college-a.example"]}, role="institution_admin")
    csv = "Student Name,USN,Course,Sem,Phone\nRavi Kumar,1MS23MBA001,MBA,1,9876543210\nAsha Rao,1MS23MBA002,MBA,1,9876543211\n".encode()
    body, content_type = multipart({"entity": "student"}, "students.csv", csv)
    job = call("/v1/ingestion/uploads", "POST", raw=body, content_type=content_type)["job"]
    for _ in range(30):
        job = call(f"/v1/ingestion/jobs/{job['job_id']}")["job"]
        if job["status"] in {"imported", "needs_review", "failed"}:
            break
        time.sleep(0.5)
    assert job["status"] == "imported", job
    attendance = "USN,Subject Code,Total Classes,Attended,Month\n1MS23MBA001,MBA101,20,12,Aug\n1MS23MBA002,MBA101,20,19,Aug\n".encode()
    body, content_type = multipart({"entity": "attendance"}, "attendance.csv", attendance)
    job = call("/v1/ingestion/uploads", "POST", raw=body, content_type=content_type)["job"]
    for _ in range(30):
        job = call(f"/v1/ingestion/jobs/{job['job_id']}")["job"]
        if job["status"] in {"imported", "needs_review", "failed"}:
            break
        time.sleep(0.5)
    assert job["status"] == "imported", job
    answer = call("/v1/agent/commands", "POST", {"command": "How many MBA students have attendance below 75%?"})
    assert answer["status"] == "complete" and "1 of 2" in answer["answer"], answer
    report = call("/v1/agent/commands", "POST", {"command": "Create an excel report of MBA students below 75% attendance"})
    assert report["artifacts"], report
    download = call(report["artifacts"][0]["download_path"])
    assert download[:2] == b"PK"
    summary = call("/v1/data/summary")
    assert summary["record_counts"]["student"] >= 2
    print("PLATFORM_SMOKE_OK")


if __name__ == "__main__":
    main()
