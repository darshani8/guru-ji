import unittest

from app.config.source_registry import SourceDefinition, SourceRegistry
from app.connectors.college_a.connector import CollegeADemoConnector
from app.connectors.registry import ConnectorRegistry
from app.domain.audit import AuditOutcome
from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.domain.requests import ChatRequest, InteractionChannel
from app.orchestration.assistant_service import AssistantService
from app.persistence.database import InMemoryControlStore
from app.tools.college_tools import COLLEGE_TOOLS
from app.tools.health_tools import HEALTH_TOOLS
from app.tools.registry import ToolRegistry


def build_service():
    sources = SourceRegistry((SourceDefinition(
        source_id="college_a_demo", institution_id="college_a", display_name="College A",
        connector_type="in_memory_demo", allowed_tools=(
            "institution.overview", "institution.attendance_summary", "institution.source_health",
        ),
    ),))
    tools = ToolRegistry((*COLLEGE_TOOLS, *HEALTH_TOOLS))
    connectors = ConnectorRegistry((CollegeADemoConnector(),))
    store = InMemoryControlStore()
    return AssistantService(sources, tools, connectors, store), store


def request(prompt: str, source_ids: tuple[str, ...] = (), college_id: str = "college_a", department_id: str | None = None, batch_id: str | None = None) -> ChatRequest:
    return ChatRequest(
        request_id="req-test", principal_id="student-1", prompt=prompt,
        institution_scope=InstitutionScope(college_id, department_id, batch_id), source_ids=source_ids,
        conversation_id=None, channel=InteractionChannel.TEXT,
    )


def student(college_id: str = "college_a", department_id: str | None = None) -> Principal:
    return Principal(
        principal_id="student-1", principal_type=PrincipalType.STUDENT,
        capabilities=frozenset({Capability.ASK_READ_ONLY}),
        scopes=(InstitutionScope(college_id, department_id),), authenticated=True,
    )


class LocalVerticalSliceTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_is_cited_and_warns_demo_data(self):
        service, store = build_service()
        answer = await service.ask(request("Give me the institution overview"), student())
        self.assertEqual(answer.status, "complete")
        self.assertIn("1240", answer.answer)
        self.assertTrue(answer.citations)
        self.assertTrue(any(item["code"] == "demo_data" for item in answer.warnings))
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.SUCCESS)

    async def test_health_requires_explicit_metadata_capability_and_is_denied_in_audit(self):
        service, store = build_service()
        answer = await service.ask(request("Is the source health healthy?"), student())
        self.assertEqual(answer.status, "refused")
        self.assertEqual(answer.refusal_reason, "No approved source returned a usable result.")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)

    async def test_faculty_can_read_health_metadata(self):
        service, store = build_service()
        principal = Principal(
            principal_id="faculty-1", principal_type=PrincipalType.FACULTY,
            capabilities=frozenset({Capability.VIEW_SOURCE_METADATA}),
            scopes=(InstitutionScope("college_a"),), authenticated=True,
        )
        answer = await service.ask(request("Is the source healthy?"), principal)
        self.assertEqual(answer.status, "complete")
        self.assertIn("healthy", answer.answer)
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.SUCCESS)

    async def test_unknown_explicit_source_is_refused_before_connector_execution(self):
        service, store = build_service()
        answer = await service.ask(request("Give me the overview", ("unknown-source",)), student())
        self.assertEqual(answer.status, "refused")
        self.assertIn("unknown-source", answer.refusal_reason or "")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)

    async def test_requested_college_without_an_approved_source_is_refused(self):
        service, store = build_service()
        answer = await service.ask(request("Give me the overview", college_id="college_b"), student("college_b"))
        self.assertEqual(answer.status, "refused")
        self.assertIn("college_b", answer.refusal_reason or "")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)

    async def test_department_scope_is_not_widened_to_another_department(self):
        service, store = build_service()
        answer = await service.ask(
            request("Give me the overview", department_id="dept-ece"),
            student(department_id="dept-cse"),
        )
        self.assertEqual(answer.status, "refused")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)

    async def test_demo_tools_refuse_even_a_matching_department_scope(self):
        service, store = build_service()
        answer = await service.ask(
            request("Give me the overview", department_id="dept-cse"),
            student(department_id="dept-cse"),
        )
        self.assertEqual(answer.status, "refused")
        self.assertIn("Department scope", answer.refusal_reason or "")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)

    async def test_demo_tools_refuse_batch_scope_until_a_batch_aware_connector_exists(self):
        service, store = build_service()
        answer = await service.ask(
            request("Give me the overview", batch_id="batch-2027"),
            student(),
        )
        self.assertEqual(answer.status, "refused")
        self.assertIn("Batch scope", answer.refusal_reason or "")
        self.assertEqual(store.recent_audit()[0].outcome, AuditOutcome.DENIED)


if __name__ == "__main__":
    unittest.main()
