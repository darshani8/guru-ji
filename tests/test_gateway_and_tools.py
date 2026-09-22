import unittest

from app.domain.principals import InstitutionScope, PrincipalType
from app.gateway.spec import ParameterSpec, PlatformToolSpec, RiskLevel, ToolArgumentError, ToolCallContext, ToolOutput
from app.domain.principals import Capability
from platform_fixtures import PlatformFixture, principal


class ToolSpecTests(unittest.TestCase):
    def test_argument_validation_is_closed_and_coercing(self):
        async def handler(context, args):
            return ToolOutput(data=args)

        spec = PlatformToolSpec(name="demo_tool", description="d", group="g", required_capability=Capability.ASK_READ_ONLY, handler=handler, parameters=(
            ParameterSpec("threshold", "number", "t", minimum=1, maximum=100), ParameterSpec("limit", "integer", "l", required=True, maximum=10), ParameterSpec("format", "string", "f", enum=("csv", "pdf")), ParameterSpec("ids", "array", "i", max_length=2), ParameterSpec("flag", "boolean", "b"),
        ))
        validated = spec.validate_arguments({"threshold": "75%", "limit": "5", "format": "CSV", "ids": "a, b", "flag": "yes"})
        self.assertEqual(validated, {"threshold": 75.0, "limit": 5, "format": "CSV", "ids": ["a", "b"], "flag": True})
        for bad in ({"limit": 5, "extra": 1}, {"threshold": 5}, {"limit": 50}, {"limit": 1, "format": "xml"}, {"limit": 1, "ids": ["a", "b", "c"]}, {"limit": True}):
            with self.assertRaises(ToolArgumentError):
                spec.validate_arguments(bad)
        schema = spec.json_schema()
        self.assertEqual(schema["parameters"]["required"], ["limit"])
        self.assertEqual(schema["risk"], "read")
        with self.assertRaises(ValueError):
            PlatformToolSpec(name="Bad Name", description="d", group="g", required_capability=Capability.ASK_READ_ONLY, handler=handler)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture()
        self.hod = principal(PrincipalType.HOD)
        self.ctx = ToolCallContext("req-1", self.hod, InstitutionScope("college_a"))

    async def test_denials_are_audited_and_explained(self):
        student = principal(PrincipalType.STUDENT)
        denied = await self.fx.gateway.invoke("find_low_attendance", {}, ToolCallContext("r", student, InstitutionScope("college_a")))
        self.assertEqual(denied.status, "denied")
        self.assertIn("attendance:read", denied.denial_reason)
        out_of_scope = await self.fx.gateway.invoke("count_students", {}, ToolCallContext("r", self.hod, InstitutionScope("college_b")))
        self.assertEqual(out_of_scope.status, "denied")
        unknown = await self.fx.gateway.invoke("drop_everything", {}, self.ctx)
        self.assertEqual(unknown.status, "unknown_tool")
        invalid = await self.fx.gateway.invoke("count_students", {"bogus": 1}, self.ctx)
        self.assertEqual(invalid.status, "invalid_arguments")
        outcomes = [event.outcome.value for event in self.fx.control.recent_audit(4)]
        self.assertEqual(outcomes, ["denied"] * 4)

    async def test_data_minimization_depends_on_capability(self):
        rows_hod = (await self.fx.gateway.invoke("find_students", {"program": "MBA", "limit": 1}, self.ctx)).data["students"][0]
        self.assertIn("phone", rows_hod)
        faculty = principal(PrincipalType.FACULTY)
        rows_faculty = (await self.fx.gateway.invoke("find_students", {"program": "MBA", "limit": 1}, ToolCallContext("r", faculty, InstitutionScope("college_a")))).data["students"][0]
        self.assertNotIn("phone", rows_faculty)
        self.assertNotIn("email", rows_faculty)
        self.assertEqual(rows_faculty["student_id"], "MBA001")

    async def test_low_attendance_pending_fees_and_exam_tools(self):
        low = await self.fx.gateway.invoke("find_low_attendance", {"program": "mba", "threshold": "75"}, self.ctx)
        self.assertEqual(low.status, "success")
        self.assertEqual([item["student_id"] for item in low.data["students"]], ["MBA001", "MBA002"])
        self.assertEqual(low.provenance[0]["source_type"], "internal_database")
        fees = await self.fx.gateway.invoke("get_pending_fees", {}, self.ctx)
        self.assertEqual((fees.data["count"], fees.data["total_outstanding"]), (1, 60000.0))
        exams = await self.fx.gateway.invoke("get_exam_summary", {"program": "MBA"}, self.ctx)
        self.assertEqual(exams.data["pass_rate_percent"], 50.0)
        summary = await self.fx.gateway.invoke("get_institution_summary", {}, self.ctx)
        self.assertEqual(summary.data["record_counts"]["student"], 8)

    async def test_report_email_and_notification_actions(self):
        low = await self.fx.gateway.invoke("find_low_attendance", {"program": "MBA"}, self.ctx)
        report = await self.fx.gateway.invoke("generate_report", {"title": "Low attendance", "format": "pdf", "rows": low.data["students"]}, self.ctx)
        self.assertEqual(report.status, "success")
        self.assertEqual(report.artifacts[0]["type"], "report")
        record, content = self.fx.reports.fetch(self.hod, "college_a", report.data["report_id"])
        self.assertTrue(content.startswith(b"%PDF"))
        hod = await self.fx.gateway.invoke("find_hod", {"program": "MBA"}, self.ctx)
        self.assertEqual(hod.data["hods"][0]["faculty_id"], "F01")
        email = await self.fx.gateway.invoke("send_email", {"recipients": ["F01", "outsider@gmail.com", "dean@abc.edu.in"], "subject": "Report", "body": "Attached", "report_ids": [report.data["report_id"]]}, self.ctx)
        self.assertEqual(email.status, "success")
        self.assertEqual([item["email"] for item in email.data["recipients"]], ["meena@abc.edu.in", "dean@abc.edu.in"])
        self.assertEqual(email.data["unresolved_recipients"], ["outsider@gmail.com"])
        self.assertEqual(self.fx.email_sender.sent[0].attachments[0].file_name, "low_attendance.pdf")
        note = await self.fx.gateway.invoke("create_notification", {"title": "Done", "body": "Report sent"}, self.ctx)
        self.assertEqual(note.data["recipients"], [self.hod.principal_id])
        self.assertEqual(self.fx.notifications.inbox(self.hod, "college_a")[0]["title"], "Done")

    async def test_high_risk_tools_need_a_matching_single_use_approval(self):
        pri = principal(PrincipalType.PRINCIPAL)
        ctx = ToolCallContext("r1", pri, InstitutionScope("college_a"))
        first = await self.fx.gateway.invoke("update_student_record", {"student_id": "MBA001", "changes": {"semester": 2}}, ctx)
        self.assertEqual(first.status, "approval_required")
        approval_id = first.approval["approval_id"]
        with self.assertRaises(PermissionError):
            self.fx.gateway.decide_approval(principal(PrincipalType.PRINCIPAL, "other"), "college_a", approval_id, approve=True)
        self.fx.gateway.decide_approval(pri, "college_a", approval_id, approve=True)
        tampered = await self.fx.gateway.invoke("update_student_record", {"student_id": "MBA001", "changes": {"semester": 9}}, ToolCallContext("r2", pri, InstitutionScope("college_a"), approval_id=approval_id))
        self.assertEqual(tampered.status, "approval_required", "different arguments must not reuse an approval")
        done = await self.fx.gateway.invoke("update_student_record", {"student_id": "MBA001", "changes": {"semester": 2}}, ToolCallContext("r3", pri, InstitutionScope("college_a"), approval_id=approval_id))
        self.assertEqual(done.status, "success")
        self.assertEqual(done.data["after"]["semester"], 2)
        replay = await self.fx.gateway.invoke("update_student_record", {"student_id": "MBA001", "changes": {"semester": 2}}, ToolCallContext("r4", pri, InstitutionScope("college_a"), approval_id=approval_id))
        self.assertEqual(replay.status, "approval_required", "approvals are single use")
        with self.assertRaises(ValueError):
            self.fx.reports.generate(pri, "college_a", title="x", columns=[], rows=[])

    def test_registry_filters_tools_by_principal(self):
        names = {tool.name for tool in self.fx.registry.for_principal(principal(PrincipalType.STUDENT))}
        self.assertNotIn("find_students", names)
        self.assertIn("search_documents", names)
        self.assertEqual(self.fx.registry.get("send_email").risk, RiskLevel.WRITE)
        self.assertEqual(self.fx.registry.get("update_student_record").risk, RiskLevel.HIGH_RISK)


if __name__ == "__main__":
    unittest.main()
