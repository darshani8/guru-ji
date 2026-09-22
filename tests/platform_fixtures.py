"""Shared in-memory platform fixture for tests."""

from __future__ import annotations

from app.actions.email import EmailService, OutboxEmailSender
from app.actions.notifications import NotificationService
from app.actions.reports import ReportService
from app.agents.master import MasterAgent
from app.auth.roles import capabilities_for_role
from app.data_access.service import InstitutionDataService
from app.documents.rag import DocumentRagService
from app.domain.principals import InstitutionScope, Principal, PrincipalType
from app.gateway.gateway import ToolGateway
from app.ingestion.registry import ParserRegistry
from app.ingestion.service import IngestionService
from app.institution_data.models import CanonicalRecord
from app.institution_data.store import InstitutionDataStore
from app.persistence.database import InMemoryControlStore
from app.platform_tools.build import build_platform_registry
from app.platform_tools.context import PlatformServices
from app.storage.object_store import InMemoryObjectStore
from app.workers.handlers import register_handlers
from app.workers.queue import InlineJobQueue


def principal(role: PrincipalType, principal_id: str | None = None, college_id: str = "college_a") -> Principal:
    return Principal(principal_id or f"{role.value}-1", role, capabilities_for_role(role), (InstitutionScope(college_id),))


class PlatformFixture:
    def __init__(self, *, intelligence=None, seed: bool = True):
        self.store = InstitutionDataStore(":memory:")
        self.objects = InMemoryObjectStore()
        self.control = InMemoryControlStore()
        self.parsers = ParserRegistry()
        self.data = InstitutionDataService(self.store)
        self.reports = ReportService(self.store, self.objects)
        self.email_sender = OutboxEmailSender()
        self.email = EmailService(self.store, self.objects, self.email_sender)
        self.notifications = NotificationService(self.store, self.control)
        self.documents = DocumentRagService(self.store, self.objects, self.parsers)
        self.ingestion = IngestionService(store=self.store, objects=self.objects, parsers=self.parsers)
        services = PlatformServices(store=self.store, data=self.data, reports=self.reports, email=self.email, notifications=self.notifications, documents=self.documents, intelligence=intelligence, ingestion=self.ingestion)
        self.registry = build_platform_registry(services)
        self.gateway = ToolGateway(self.registry, self.store, self.control)
        self.jobs = InlineJobQueue(self.store)
        self.agent = MasterAgent(self.gateway, self.registry, self.data, self.store, self.control, background=self.jobs)
        register_handlers(self.jobs, ingestion=self.ingestion, agent=self.agent, notifications=self.notifications)
        if seed:
            self.seed()

    def seed(self) -> None:
        self.store.upsert_institution("college_a", "ABC College", location="Bengaluru", settings={"email_domains": ["abc.edu.in"]})
        self.store.upsert_records("college_a", [CanonicalRecord("student", {"student_id": f"MBA00{i}", "name": f"Student {i}", "program": "MBA", "semester": 1, "phone": "9876543210", "email": f"s{i}@x.com"}) for i in range(1, 6)] + [CanonicalRecord("student", {"student_id": f"BCA00{i}", "name": f"Bca {i}", "program": "BCA", "semester": 3}) for i in range(1, 4)])
        self.store.upsert_records("college_a", [CanonicalRecord("attendance", {"student_id": f"MBA00{i}", "course_code": "C1", "period": "2026-08", "classes_held": 20, "classes_attended": 10 + i * 2}) for i in range(1, 6)])
        self.store.upsert_records("college_a", [CanonicalRecord("faculty", {"faculty_id": "F01", "name": "Dr Meena", "designation": "Professor & HOD", "department": "MBA", "email": "meena@abc.edu.in", "is_hod": True}), CanonicalRecord("faculty", {"faculty_id": "F02", "name": "Dr Prakash", "designation": "Principal", "department": "Administration", "email": "principal@abc.edu.in"})])
        self.store.upsert_records("college_a", [CanonicalRecord("fee", {"student_id": "MBA001", "fee_type": "Tuition", "amount_due": 100000, "amount_paid": 40000, "semester": 1}), CanonicalRecord("fee", {"student_id": "MBA002", "fee_type": "Tuition", "amount_due": 100000, "amount_paid": 100000, "semester": 1})])
        self.store.upsert_records("college_a", [CanonicalRecord("exam", {"student_id": "MBA001", "course_code": "C1", "exam_name": "IA1", "marks_obtained": 30, "max_marks": 50, "result_status": "pass"}), CanonicalRecord("exam", {"student_id": "MBA002", "course_code": "C1", "exam_name": "IA1", "marks_obtained": 15, "max_marks": 50, "result_status": "fail"})])
