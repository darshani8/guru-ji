import json
import unittest
from datetime import datetime, timezone

import httpx

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings
from app.connectors.base import ConnectorContext
from app.connectors.remote_http import RemoteHttpConnector
from app.domain.principals import InstitutionScope, PrincipalType
from app.domain.results import ResultStatus
from app.policy.query_limits import QueryLimits


class RemoteConnectorTests(unittest.IsolatedAsyncioTestCase):
    def connector(self, transport, **overrides):
        values = {
            "source_id": "college_a_remote",
            "institution_id": "college_a",
            "display_name": "College A remote",
            "base_url": "https://connector.example.test",
            "allowed_tools": frozenset({"institution.overview", "institution.attendance_summary"}),
            "auth_token": "connector-secret",
            "transport": transport,
        }
        values.update(overrides)
        return RemoteHttpConnector(**values)

    async def test_execute_sends_request_id_limits_and_returns_provenance(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/execute")
            self.assertEqual(request.headers["authorization"], "Bearer connector-secret")
            self.assertEqual(request.headers["x-request-id"], "req-remote")
            payload = json.loads(request.content)
            self.assertEqual(payload["tool_name"], "institution.overview")
            self.assertEqual(payload["principal"], {
                "id": "faculty-1",
                "type": "faculty",
                "capabilities": [],
                "scopes": [{"college_id": "college_a", "department_id": "dept_1", "batch_id": None}],
                "consent_verified": False,
                "revoked": False,
            })
            self.assertEqual(payload["institution_scope"], {"college_id": "college_a", "department_id": "dept_1", "batch_id": None})
            self.assertEqual(payload["limits"]["max_rows"], 500)
            return httpx.Response(200, json={
                "contract_version": "2",
                "tool_name": "institution.overview",
                "status": "success",
                "data": {"active_students": 500},
                "provenance": [{
                    "source_id": "college_a_remote",
                    "source_type": "internal_database",
                    "retrieved_at": "2026-09-20T00:00:00Z",
                    "complete": True,
                    "rows_used": 1,
                }],
                "warnings": [],
            })

        result = await self.connector(httpx.MockTransport(handler)).execute(
            "institution.overview",
            {},
            ConnectorContext(
                "req-remote",
                "college_a_remote",
                QueryLimits(),
                principal_id="faculty-1",
                principal_type=PrincipalType.FACULTY,
                institution_scope=InstitutionScope("college_a", "dept_1"),
            ),
        )
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.data, {"active_students": 500})
        self.assertEqual(result.provenance[0].source_id, "college_a_remote")

    async def test_health_maps_remote_health_contract(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/v1/health")
            return httpx.Response(200, json={
                "status": "healthy",
                "freshness": "current",
                "last_success_at": "2026-09-20T00:00:00Z",
                "detail": "read-only view service ready",
            })

        health = await self.connector(httpx.MockTransport(handler)).health()
        self.assertEqual(health.status.value, "healthy")
        self.assertEqual(health.freshness.value, "current")
        self.assertEqual(health.connector_type, "remote_http")
        self.assertEqual(health.last_success_at, datetime(2026, 9, 20, tzinfo=timezone.utc))

    async def test_provenance_mismatch_is_rejected(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json={
                "contract_version": "2",
                "tool_name": "institution.overview",
                "status": "success",
                "data": {"active_students": 500},
                "provenance": [{"source_id": "another_college", "source_type": "internal_database"}],
            })

        result = await self.connector(httpx.MockTransport(handler)).execute(
            "institution.overview", {}, ConnectorContext("req", "college_a_remote", QueryLimits())
        )
        self.assertEqual(result.status, ResultStatus.INVALID_RESULT)
        self.assertEqual(result.warnings[0].code, "invalid_remote_result")

    async def test_oversized_response_is_unavailable(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, headers={"content-length": "100"}, content=b"{}")

        connector = self.connector(httpx.MockTransport(handler), max_response_bytes=10)
        result = await connector.execute(
            "institution.overview", {}, ConnectorContext("req", "college_a_remote", QueryLimits())
        )
        self.assertEqual(result.status, ResultStatus.UNAVAILABLE)
        self.assertEqual(result.warnings[0].code, "source_unavailable")

    async def test_disallowed_tool_never_calls_remote_service(self):
        called = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            del request
            return httpx.Response(500)

        result = await self.connector(httpx.MockTransport(handler)).execute(
            "institution.delete_student", {}, ConnectorContext("req", "college_a_remote", QueryLimits())
        )
        self.assertEqual(result.status, ResultStatus.UNAVAILABLE)
        self.assertFalse(called)

    def test_runtime_registers_remote_source_without_demo_data(self):
        runtime = build_runtime(AppSettings(
            environment="test",
            control_database_url=":memory:",
            demo_data_enabled=False,
            institution_connector_base_url="https://connector.example.test",
            institution_connector_auth_token="secret",
        ))
        try:
            self.assertEqual([item.source_id for item in runtime.sources.all()], ["college_a_remote"])
            self.assertTrue(runtime.tools.get("institution.overview").allows_source("college_a_remote"))
            self.assertEqual(runtime.connectors.get("college_a_remote").source_id, "college_a_remote")
        finally:
            runtime.store.close()

    def test_connector_rejects_credentials_in_base_url(self):
        with self.assertRaisesRegex(ValueError, "credentials"):
            self.connector(httpx.MockTransport(lambda request: httpx.Response(200)), base_url="https://user:pass@example.test")


if __name__ == "__main__":
    unittest.main()
