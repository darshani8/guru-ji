import json
import os
import unittest
from unittest.mock import patch

from app.api.dependencies import build_runtime
from app.config.institution_connectors import (
    InstitutionConnectorDefinition,
    parse_institution_connectors,
)
from app.config.settings import AppSettings
from app.domain.principals import InstitutionScope
from app.domain.requests import ChatRequest
from app.orchestration.plan_validator import build_tool_plan


class MultiCollegeConnectorTests(unittest.TestCase):
    def definition(self, source_id: str, institution_id: str) -> InstitutionConnectorDefinition:
        return InstitutionConnectorDefinition(
            source_id=source_id,
            institution_id=institution_id,
            display_name=f"{institution_id} connector",
            base_url=f"https://{institution_id}.connector.example.test",
            auth_token=f"token-{institution_id}",
            scope_attestation_required=True,
        )

    def test_environment_registry_resolves_per_college_secret_references(self):
        raw = json.dumps(
            [
                {
                    "source_id": "college_a_remote",
                    "institution_id": "college_a",
                    "display_name": "College A connector",
                    "base_url": "https://college-a.connector.example.test",
                    "auth_token_env": "COLLEGE_A_CONNECTOR_TOKEN",
                    "scope_attestation_required": True,
                },
                {
                    "source_id": "college_b_remote",
                    "institution_id": "college_b",
                    "display_name": "College B connector",
                    "base_url": "https://college-b.connector.example.test",
                    "auth_token_env": "COLLEGE_B_CONNECTOR_TOKEN",
                    "scope_attestation_required": True,
                },
            ]
        )
        with patch.dict(
            os.environ,
            {
                "GURU_ENVIRONMENT": "test",
                "GURU_INSTITUTION_CONNECTORS": raw,
                "COLLEGE_A_CONNECTOR_TOKEN": "a-secret",
                "COLLEGE_B_CONNECTOR_TOKEN": "b-secret",
            },
            clear=True,
        ):
            settings = AppSettings.from_env()

        configured = settings.configured_institution_connectors()
        self.assertEqual([item.institution_id for item in configured], ["college_a", "college_b"])
        self.assertEqual([item.auth_token for item in configured], ["a-secret", "b-secret"])
        self.assertTrue(all(item.scope_attestation_required for item in configured))

    def test_runtime_registers_multiple_college_sources_and_connectors(self):
        runtime = build_runtime(
            AppSettings(
                environment="test",
                control_database_url=":memory:",
                demo_data_enabled=False,
                institution_connectors=(
                    self.definition("college_a_remote", "college_a"),
                    self.definition("college_b_remote", "college_b"),
                ),
            )
        )
        try:
            self.assertEqual(
                [item.source_id for item in runtime.sources.all()],
                ["college_a_remote", "college_b_remote"],
            )
            self.assertEqual(
                [item.source_id for item in runtime.connectors.all()],
                ["college_a_remote", "college_b_remote"],
            )
            self.assertEqual(
                runtime.sources.for_institution("college_b")[0].source_id,
                "college_b_remote",
            )
            self.assertTrue(runtime.connectors.get("college_b_remote").scope_attestation_required)
        finally:
            runtime.store.close()

    def test_default_plan_routes_each_college_only_to_its_registered_source(self):
        runtime = build_runtime(
            AppSettings(
                environment="test",
                control_database_url=":memory:",
                demo_data_enabled=False,
                institution_connectors=(
                    self.definition("college_a_remote", "college_a"),
                    self.definition("college_b_remote", "college_b"),
                ),
            )
        )
        try:
            request = ChatRequest(
                request_id="req-college-b",
                principal_id="faculty-b",
                prompt="What is the attendance summary?",
                institution_scope=InstitutionScope("college_b"),
            )
            plan = build_tool_plan(request, runtime.tools, runtime.sources)
            self.assertEqual([step.source_id for step in plan], ["college_b_remote"])
        finally:
            runtime.store.close()

    def test_explicit_source_cannot_cross_college_scope(self):
        runtime = build_runtime(
            AppSettings(
                environment="test",
                control_database_url=":memory:",
                demo_data_enabled=False,
                institution_connectors=(
                    self.definition("college_a_remote", "college_a"),
                    self.definition("college_b_remote", "college_b"),
                ),
            )
        )
        try:
            request = ChatRequest(
                request_id="req-cross-college",
                principal_id="faculty-b",
                prompt="Give the institution overview.",
                institution_scope=InstitutionScope("college_b"),
                source_ids=("college_a_remote",),
            )
            with self.assertRaisesRegex(ValueError, "outside the requested institution scope"):
                build_tool_plan(request, runtime.tools, runtime.sources)
        finally:
            runtime.store.close()

    def test_duplicate_source_ids_are_rejected(self):
        raw = json.dumps(
            [
                {
                    "source_id": "same-source",
                    "institution_id": "college_a",
                    "display_name": "A",
                    "base_url": "https://a.example.test",
                },
                {
                    "source_id": "same-source",
                    "institution_id": "college_b",
                    "display_name": "B",
                    "base_url": "https://b.example.test",
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "source_id values must be unique"):
            parse_institution_connectors(raw)

    def test_only_approved_semantic_tools_are_allowed(self):
        with self.assertRaisesRegex(ValueError, "unsupported tools"):
            InstitutionConnectorDefinition(
                source_id="college_a_remote",
                institution_id="college_a",
                display_name="College A connector",
                base_url="https://college-a.connector.example.test",
                allowed_tools=("institution.delete_student",),
            )


if __name__ == "__main__":
    unittest.main()
