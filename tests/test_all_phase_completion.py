from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.domain.audit import AuditEvent
from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.domain.streaming import (
    DoneEvent,
    MessageEndEvent,
    MessageStartEvent,
    StreamSequenceError,
    StreamSequenceValidator,
)
from app.integrations.edge import OpenEdxAdapter
from app.integrations.mcp_gateway import GatewayTarget, GatewayTool, McpGatewayAllowlist
from app.observability.export import HttpJsonTraceExporter
from app.observability.tracing import TraceRecorder
from app.persistence.control_plane import AnswerEnvelopeMetadata
from app.persistence.database import SqliteControlStore
from app.persistence.outbox import OutboxDispatcher
from app.policy.authorization import DenialReason
from app.policy.cerbos import CerbosPolicyDecisionPoint
from app.voice.providers import VoiceEventNormalizer
from connector.app.main import ConnectorSettings, create_app
from connector.app.repository import DemoReportingRepository


class NewPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.principal = Principal(
            principal_id="faculty-1",
            principal_type=PrincipalType.FACULTY,
            capabilities=frozenset({Capability.ASK_READ_ONLY}),
            scopes=(InstitutionScope("college_a"),),
        )
        self.scope = InstitutionScope("college_a")

    def test_cerbos_allows_only_matching_fresh_attested_scope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(payload["resourceInstances"]["college_a_remote"]["actions"], ["retrieve"])
            return httpx.Response(200, json={
                "policyVersion": "guru-cerbos-v1",
                "callId": "decision-1",
                "policyStatus": "current",
                "resourceInstances": {
                    "college_a_remote": {
                        "actions": {
                            "retrieve": {
                                "effect": "EFFECT_ALLOW",
                                "matchedScope": {"college_id": "college_a"},
                            },
                        },
                    },
                },
            })

        pdp = CerbosPolicyDecisionPoint("http://cerbos.test", "guru-cerbos-v1", transport=httpx.MockTransport(handler))
        decision = pdp.evaluate(
            principal=self.principal,
            required_capability=Capability.ASK_READ_ONLY,
            action="retrieve",
            resource_type="connector_resource",
            resource_id="college_a_remote",
            requested_scope=self.scope,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.decision_id, "decision-1")

    def test_standard_cerbos_response_uses_pinned_sidecar_and_requested_scope(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "resourceInstances": {"college_a_remote": {"actions": {"retrieve": "EFFECT_ALLOW"}}},
        }))
        pdp = CerbosPolicyDecisionPoint(
            "http://cerbos.test", "guru-cerbos-v1", require_fresh=False, transport=transport,
        )
        decision = pdp.evaluate(
            principal=self.principal, required_capability=Capability.ASK_READ_ONLY,
            action="retrieve", resource_type="connector_resource", resource_id="college_a_remote",
            requested_scope=self.scope,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched_scope, self.scope)

    def test_cerbos_denies_stale_policy_and_unknown_action(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"policyVersion": "old"}))
        pdp = CerbosPolicyDecisionPoint("http://cerbos.test", "guru-cerbos-v1", transport=transport)
        stale = pdp.evaluate(
            principal=self.principal, required_capability=Capability.ASK_READ_ONLY,
            action="retrieve", resource_type="connector_resource", resource_id="source", requested_scope=self.scope,
        )
        self.assertFalse(stale.allowed)
        self.assertEqual(stale.reason, DenialReason.STALE_POLICY)
        unknown = pdp.evaluate(
            principal=self.principal, required_capability=Capability.ASK_READ_ONLY,
            action="delete", resource_type="connector_resource", resource_id="source", requested_scope=self.scope,
        )
        self.assertFalse(unknown.allowed)
        self.assertEqual(unknown.reason, DenialReason.UNKNOWN_ACTION)

    def test_outbox_retries_until_acknowledged_and_metadata_is_durable(self) -> None:
        store = SqliteControlStore("sqlite:///:memory:")
        store.append_audit(AuditEvent(event_id="audit-1", event_type="test", request_id="req-1"))
        store.record_answer_envelope(AnswerEnvelopeMetadata(
            request_id="req-1", conversation_id="conv-1", principal_id="faculty-1", college_id="college_a",
            status="complete", citations_count=1, warnings_count=0, answer_sha256="a" * 64,
            created_at=datetime.now(timezone.utc),
        ))
        store.enqueue_outbox("test.event", {"request_id": "req-1"})
        calls = 0

        def flaky(record):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary collector failure")

        dispatcher = OutboxDispatcher(store, flaky)
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch_once()
        self.assertEqual(len(store.drain_outbox()), 1)
        self.assertEqual(dispatcher.dispatch_once(), 1)
        self.assertEqual(store.drain_outbox(), ())
        store.close()

    def test_connector_requires_server_token_scope_and_returns_attestation(self) -> None:
        settings = ConnectorSettings()
        settings.service_token = "service-secret"
        settings.source_id = "college_a_remote"
        app = create_app(settings, DemoReportingRepository(settings.source_id, settings.institution_id))
        client = TestClient(app)
        body = {
            "contract_version": "2",
            "source_id": "college_a_remote",
            "tool_name": "institution.overview",
            "request_id": "req-1",
            "principal": {
                "id": "faculty-1", "type": "faculty", "capabilities": ["ask:read_only"],
                "scopes": [{"college_id": "college_a"}],
            },
            "institution_scope": {"college_id": "college_a"},
        }
        denied = client.post("/v1/execute", json=body)
        self.assertEqual(denied.status_code, 401)
        response = client.post("/v1/execute", headers={"Authorization": "Bearer service-secret"}, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["effective_scope"]["college_id"], "college_a")
        self.assertTrue(response.json()["provenance"][0]["redactions_applied"])

    def test_edge_mcp_voice_and_trace_adapters_are_allowlisted_and_redacted(self) -> None:
        edge_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "sub": "student-1", "role": "student", "consent_verified": True,
            "institution_scope": {"college_id": "college_a", "batch_id": "batch-1"},
        }))
        principal = OpenEdxAdapter("https://edge.test", "edge-secret", transport=edge_transport).resolve("session-proof")
        self.assertEqual(principal.principal_type, PrincipalType.STUDENT)
        self.assertIn(Capability.ASK_READ_ONLY, principal.capabilities)
        allowlist = McpGatewayAllowlist((GatewayTarget(
            "reports", "https://gateway.test", "gateway-secret",
            (GatewayTool("reports", "attendance.summary", Capability.ASK_READ_ONLY),),
        ),))
        allowlist.resolve("reports", "attendance.summary")
        with self.assertRaises(PermissionError):
            allowlist.resolve("reports", "delete.records")
        voice = VoiceEventNormalizer().normalize("livekit", "transcription.final", {"session_id": "s1", "transcript": "hello", "audio": "must-not-persist"})
        self.assertEqual(voice.event.value, "transcript.final")
        self.assertNotIn("audio", dict(voice.metadata))
        captured = []
        exporter = HttpJsonTraceExporter("https://collector.test/v1/traces", transport=httpx.MockTransport(lambda request: captured.append(request) or httpx.Response(200)))
        TraceRecorder(exporter=exporter).record("assistant.completed", trace_id="req-1", attributes={"status": "complete", "prompt": "secret", "output": "secret"})
        self.assertEqual(len(captured), 1)
        self.assertNotIn("secret", captured[0].content.decode())

    def test_stream_validator_requires_contiguous_terminal_order(self) -> None:
        validator = StreamSequenceValidator()
        validator.accept(MessageStartEvent(request_id="r", sequence=1))
        validator.accept(MessageEndEvent(request_id="r", sequence=2, status="complete"))
        validator.accept(DoneEvent(request_id="r", sequence=3))
        with self.assertRaises(StreamSequenceError):
            validator.accept(DoneEvent(request_id="r", sequence=4))
        with self.assertRaises(StreamSequenceError):
            StreamSequenceValidator().accept(DoneEvent(request_id="r", sequence=2))


if __name__ == "__main__":
    unittest.main()
