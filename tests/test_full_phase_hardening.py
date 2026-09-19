import unittest

from fastapi.testclient import TestClient

from app.auth.principal import principal_from_headers
from app.config.settings import AppSettings
from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.domain.streaming import AnswerEvent, DoneEvent, to_sse
from app.main import app
from app.observability.tracing import TraceRecorder
from app.policy.pdp import LocalPolicyDecisionPoint


class FullPhaseHardeningTests(unittest.TestCase):
    def test_demo_identity_requires_bearer_even_with_demo_headers(self):
        settings = AppSettings(environment="development", dev_bearer_token="dev-token")
        anonymous = principal_from_headers(
            {"x-demo-principal": "spoofed", "x-demo-role": "main_admin", "x-demo-college": "college_a"},
            settings,
        )
        self.assertFalse(anonymous.authenticated)
        self.assertEqual(anonymous.principal_type, PrincipalType.ANONYMOUS)

    def test_local_pdp_is_deny_by_default_and_scope_bound(self):
        principal = Principal(
            principal_id="faculty-1",
            principal_type=PrincipalType.FACULTY,
            capabilities=frozenset({Capability.ASK_READ_ONLY}),
            scopes=(InstitutionScope("college_a", "dept_1"),),
        )
        pdp = LocalPolicyDecisionPoint()
        allowed = pdp.evaluate(
            principal=principal,
            required_capability=Capability.ASK_READ_ONLY,
            action="retrieve",
            resource_type="connector_resource",
            resource_id="college_a_remote",
            requested_scope=InstitutionScope("college_a", "dept_1"),
        )
        denied = pdp.evaluate(
            principal=principal,
            required_capability=Capability.ASK_READ_ONLY,
            action="delete",
            resource_type="connector_resource",
            resource_id="college_a_remote",
            requested_scope=InstitutionScope("college_a", "dept_1"),
        )
        cross_scope = pdp.evaluate(
            principal=principal,
            required_capability=Capability.ASK_READ_ONLY,
            action="retrieve",
            resource_type="connector_resource",
            resource_id="college_b_remote",
            requested_scope=InstitutionScope("college_b"),
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)
        self.assertFalse(cross_scope.allowed)
        self.assertEqual(pdp.policy_version, "guru-local-v1")

    def test_stream_event_contract_is_closed_and_versioned(self):
        answer = to_sse(AnswerEvent(request_id="req-1", sequence=1, answer={"status": "complete"}))
        done = to_sse(DoneEvent(request_id="req-1", sequence=2))
        self.assertIn('"schema_version":"1"', answer)
        self.assertIn('"type":"answer"', answer)
        self.assertIn("event: done", done)
        with self.assertRaises(Exception):
            AnswerEvent.model_validate({
                "type": "answer",
                "request_id": "req-1",
                "sequence": 1,
                "answer": {},
                "unexpected": True,
            })

    def test_trace_recorder_drops_sensitive_attributes(self):
        recorder = TraceRecorder()
        span = recorder.record(
            "assistant.request",
            trace_id="req-1",
            attributes={
                "request_id": "req-1",
                "source_id": "college_a_demo",
                "prompt": "private learner prompt",
                "raw_audio": "secret bytes",
            },
        )
        names = {name for name, _ in span.attributes}
        self.assertIn("request_id", names)
        self.assertIn("source_id", names)
        self.assertNotIn("prompt", names)
        self.assertNotIn("raw_audio", names)

    def test_briefings_are_capability_and_scope_bound(self):
        client = TestClient(app)
        student_headers = {
            "Authorization": "Bearer dev-token",
            "X-Demo-Principal": "student-1",
            "X-Demo-Role": "student",
            "X-Demo-College": "college_a",
        }
        denied = client.post(
            "/v1/briefings/daily",
            headers=student_headers,
            json={"college_id": "college_a"},
        )
        self.assertEqual(denied.status_code, 403)

        admin_headers = {
            **student_headers,
            "X-Demo-Principal": "admin-1",
            "X-Demo-Role": "main_admin",
            "X-Demo-Capabilities": "ask:read_only,briefing:run,briefing:view_history",
        }
        allowed = client.post(
            "/v1/briefings/daily",
            headers=admin_headers,
            json={"college_id": "college_a"},
        )
        self.assertEqual(allowed.status_code, 200)
        recent = client.get("/v1/briefings/recent", headers=admin_headers)
        self.assertEqual(recent.status_code, 200)
        self.assertTrue(recent.json()["briefings"])


if __name__ == "__main__":
    unittest.main()
