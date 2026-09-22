import csv
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from app.actions.files import render_csv, render_xlsx
from app.actions.reports import ACCESS_RECORD_NAME, required_capabilities_for
from app.data_access.service import InstitutionDataService
from app.domain.principals import InstitutionScope, PrincipalType
from app.gateway.gateway import HANDLER_FAILURE_MESSAGE
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

    def test_object_parameters_reject_nested_containers(self):
        async def handler(context, args):
            return ToolOutput(data=args)

        spec = PlatformToolSpec(name="obj_tool", description="d", group="g", required_capability=Capability.ASK_READ_ONLY, handler=handler, parameters=(ParameterSpec("changes", "object", "c", required=True, max_length=3),))
        self.assertEqual(spec.validate_arguments({"changes": {"phone": 9876543210, "section": "A", "flag": True}})["changes"], {"phone": 9876543210, "section": "A", "flag": True})
        for bad in ({"address": {"line1": "x"}}, {"tags": ["a"]}, {"pair": ("a", "b")}, {"": "blank key"}, {"a": 1, "b": 2, "c": 3, "d": 4}):
            with self.assertRaises(ToolArgumentError):
                spec.validate_arguments({"changes": bad})
        with self.assertRaises(ToolArgumentError):
            spec.validate_arguments({"changes": "not an object"})


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

    async def test_handler_exceptions_are_audited_and_never_leak_details(self):
        async def boom(context, args):
            raise RuntimeError("connection to db-internal:5432 failed: password=hunter2")

        self.fx.registry.register(PlatformToolSpec(name="boom_tool", description="d", group="g", required_capability=Capability.ASK_READ_ONLY, handler=boom))
        with self.assertLogs("app.gateway.gateway", level="ERROR") as logs:
            result = await self.fx.gateway.invoke("boom_tool", {}, self.ctx)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.denial_reason, HANDLER_FAILURE_MESSAGE)
        self.assertNotIn("hunter2", json.dumps(result.as_dict()))
        self.assertIn("hunter2", "\n".join(logs.output), "the traceback goes to the log")
        event = self.fx.control.recent_audit(1)[0]
        self.assertEqual((event.outcome.value, event.tool_names), ("failed", ("boom_tool",)))
        self.assertIn(("reason", "handler_exception"), event.decision_metadata)

    async def test_high_risk_approval_survives_a_handler_failure(self):
        pri = principal(PrincipalType.PRINCIPAL)
        args = {"student_id": "MBA001", "changes": {"section": "B"}}
        first = await self.fx.gateway.invoke("update_student_record", args, ToolCallContext("r1", pri, InstitutionScope("college_a")))
        approval_id = first.approval["approval_id"]
        self.fx.gateway.decide_approval(pri, "college_a", approval_id, approve=True)
        ctx = ToolCallContext("r2", pri, InstitutionScope("college_a"), approval_id=approval_id)
        with patch.object(InstitutionDataService, "update_student", side_effect=RuntimeError("database unavailable")), self.assertLogs("app.gateway.gateway", level="ERROR"):
            failed = await self.fx.gateway.invoke("update_student_record", args, ctx)
        self.assertEqual((failed.status, failed.denial_reason), ("failed", HANDLER_FAILURE_MESSAGE))
        self.assertEqual(self.fx.store.get_approval("college_a", approval_id)["status"], "approved", "a failed handler must not burn the confirmation")
        done = await self.fx.gateway.invoke("update_student_record", args, ToolCallContext("r3", pri, InstitutionScope("college_a"), approval_id=approval_id))
        self.assertEqual(done.status, "success")
        self.assertEqual(self.fx.store.get_approval("college_a", approval_id)["status"], "consumed")

    async def test_student_update_values_are_validated_before_an_approval_exists(self):
        pri = principal(PrincipalType.PRINCIPAL)
        ctx = ToolCallContext("r1", pri, InstitutionScope("college_a"))
        for changes, fragment in (
            ({"email": None}, "email"), ({"address": {"line1": "x"}}, "address"), ({"section": ""}, "section"),
            ({"phone": "call me"}, "phone"), ({"email": "not-an-email"}, "email"), ({"name": "x"}, "name"), ({}, "at least one"),
        ):
            result = await self.fx.gateway.invoke("update_student_record", {"student_id": "MBA001", "changes": changes}, ctx)
            self.assertEqual(result.status, "invalid_arguments", changes)
            self.assertIn(fragment, result.denial_reason)
        self.assertEqual(self.fx.store.list_approvals("college_a", status=None), [], "rejected values never create an approval")
        raw = {"student_id": "MBA001", "changes": {"phone": 9876543210, "section": True, "email": " New@ABC.edu.in ", "semester": 2.0}}
        pending = await self.fx.gateway.invoke("update_student_record", raw, ctx)
        self.assertEqual(pending.status, "approval_required")
        self.assertEqual(pending.approval["arguments"]["changes"], {"phone": "9876543210", "section": "True", "email": "new@abc.edu.in", "semester": 2})
        approval_id = pending.approval["approval_id"]
        self.fx.gateway.decide_approval(pri, "college_a", approval_id, approve=True)
        done = await self.fx.gateway.invoke("update_student_record", raw, ToolCallContext("r2", pri, InstitutionScope("college_a"), approval_id=approval_id))
        self.assertEqual(done.status, "success")
        stored = self.fx.store.get_record("college_a", "student", "mba001")
        self.assertEqual((stored["phone"], stored["section"], stored["email"], stored["semester"]), ("9876543210", "True", "new@abc.edu.in", 2))
        self.assertIsInstance(stored["phone"], str)

    async def test_institution_summary_indicators_follow_capabilities(self):
        student = principal(PrincipalType.STUDENT)
        summary = await self.fx.gateway.invoke("get_institution_summary", {}, ToolCallContext("r", student, InstitutionScope("college_a")))
        self.assertEqual(summary.status, "success")
        self.assertNotIn("attendance", summary.data)
        self.assertNotIn("fees", summary.data)
        self.assertNotIn("%", summary.summary)
        self.assertNotIn("Outstanding", summary.summary)
        self.assertEqual(summary.data["record_counts"]["student"], 8)
        self.assertEqual(summary.data["programs"], ["BCA", "MBA"])
        faculty = await self.fx.gateway.invoke("get_institution_summary", {}, ToolCallContext("r", principal(PrincipalType.FACULTY), InstitutionScope("college_a")))
        self.assertIn("attendance", faculty.data)
        self.assertNotIn("fees", faculty.data, "faculty lack fees:read")
        staff = await self.fx.gateway.invoke("get_institution_summary", {}, ToolCallContext("r", principal(PrincipalType.STAFF), InstitutionScope("college_a")))
        self.assertNotIn("attendance", staff.data, "staff lack attendance:read")
        self.assertIn("fees", staff.data)
        hod = await self.fx.gateway.invoke("get_institution_summary", {}, self.ctx)
        self.assertEqual(hod.data["attendance"]["below_75_percent"], 2)
        self.assertEqual(hod.data["fees"]["total_outstanding"], 60000.0)

    async def test_reports_are_minimised_and_gated_by_their_data(self):
        self.assertEqual(required_capabilities_for(["student_id", "name", "phone", "attendance_percent", "balance", "grade"]), ("attendance:read", "exams:read", "fees:read", "students:read_contact"))
        self.assertEqual(required_capabilities_for(["student_id", "name", "program"]), ())
        students = (await self.fx.gateway.invoke("find_students", {"program": "MBA", "limit": 2}, self.ctx)).data["students"]
        self.assertIn("phone", students[0])
        hod_report = await self.fx.gateway.invoke("generate_report", {"title": "Contacts", "format": "csv", "rows": students}, self.ctx)
        self.assertEqual(hod_report.status, "success")
        report_id = hod_report.data["report_id"]
        _, content = self.fx.reports.fetch(self.hod, "college_a", report_id)
        self.assertIn(b"9876543210", content, "the creator keeps the data they were allowed to see")
        for role in (PrincipalType.FACULTY, PrincipalType.STAFF):
            other = principal(role)
            with self.assertRaises(PermissionError):
                self.fx.reports.fetch(other, "college_a", report_id)
            self.assertNotIn(report_id, [row["report_id"] for row in self.fx.reports.list(other, "college_a")])
        listed = await self.fx.gateway.invoke("list_reports", {}, ToolCallContext("r", principal(PrincipalType.FACULTY), InstitutionScope("college_a")))
        self.assertEqual(listed.data["count"], 0)
        principal_record, _ = self.fx.reports.fetch(principal(PrincipalType.PRINCIPAL), "college_a", report_id)
        self.assertEqual(principal_record["report_id"], report_id, "a principal holds every capability the report needs")
        # A faculty member generating from the same rows never gets contact data into the file.
        faculty = principal(PrincipalType.FACULTY)
        faculty_ctx = ToolCallContext("r", faculty, InstitutionScope("college_a"))
        faculty_report = await self.fx.gateway.invoke("generate_report", {"title": "Roster", "format": "csv", "columns": ["student_id", "name", "phone", "email"], "rows": students}, faculty_ctx)
        self.assertEqual(faculty_report.status, "success")
        record, content = self.fx.reports.fetch(faculty, "college_a", faculty_report.data["report_id"])
        self.assertNotIn(b"9876543210", content)
        self.assertNotIn(b"s1@x.com", content)
        self.assertTrue(content.decode("utf-8-sig").startswith("student_id,name\n"))
        self.assertEqual(self.fx.reports.required_capabilities(record), ())
        staff = principal(PrincipalType.STAFF)
        self.assertEqual(self.fx.reports.fetch(staff, "college_a", faculty_report.data["report_id"])[0]["report_id"], faculty_report.data["report_id"])
        contact_only = await self.fx.gateway.invoke("generate_report", {"title": "Phones", "format": "csv", "columns": ["phone"], "rows": students}, faculty_ctx)
        self.assertEqual(contact_only.status, "failed")
        # Attendance columns require attendance:read for anyone but the creator.
        low = (await self.fx.gateway.invoke("find_low_attendance", {"program": "MBA"}, self.ctx)).data["students"]
        attendance_report = await self.fx.gateway.invoke("generate_report", {"title": "Low attendance", "format": "xlsx", "rows": low}, self.ctx)
        with self.assertRaises(PermissionError):
            self.fx.reports.fetch(staff, "college_a", attendance_report.data["report_id"])
        self.assertEqual(self.fx.reports.fetch(faculty, "college_a", attendance_report.data["report_id"])[0]["report_id"], attendance_report.data["report_id"])
        # A report whose access record is missing is visible to its creator only.
        self.assertTrue(self.fx.objects.delete(record["object_key"].rsplit("/", 1)[0] + "/" + ACCESS_RECORD_NAME))
        self.assertIsNone(self.fx.reports.required_capabilities(record))
        with self.assertRaises(PermissionError):
            self.fx.reports.fetch(staff, "college_a", faculty_report.data["report_id"])
        self.assertEqual(self.fx.reports.fetch(faculty, "college_a", faculty_report.data["report_id"])[0]["report_id"], faculty_report.data["report_id"])

    def test_csv_cells_cannot_start_a_formula(self):
        rows = [
            {"name": "=HYPERLINK(\"http://evil\")", "note": "+cmd|' /C calc'!A0", "amount": -5, "ratio": -0.5, "flag": True, "at": "@SUM(1)", "tab": "\tx", "cr": "\rx", "plain": "-dash text", "empty": None, "safe": "Student 1"},
        ]
        columns = ["name", "note", "amount", "ratio", "flag", "at", "tab", "cr", "plain", "empty", "safe", "=header"]
        parsed = list(csv.reader(io.StringIO(render_csv(columns, rows).decode("utf-8-sig"))))
        self.assertEqual(parsed[0], ["name", "note", "amount", "ratio", "flag", "at", "tab", "cr", "plain", "empty", "safe", "'=header"])
        self.assertEqual(parsed[1], ["'=HYPERLINK(\"http://evil\")", "'+cmd|' /C calc'!A0", "-5", "-0.5", "Yes", "'@SUM(1)", "'\tx", "'\rx", "'-dash text", "", "Student 1", ""])
        sheet = zipfile.ZipFile(io.BytesIO(render_xlsx(columns, rows))).read("xl/worksheets/sheet1.xml")
        self.assertIn(b'<t xml:space="preserve">=HYPERLINK("http://evil")</t>', sheet, "xlsx inline strings are already inert and stay unchanged")

    def test_registry_filters_tools_by_principal(self):
        names = {tool.name for tool in self.fx.registry.for_principal(principal(PrincipalType.STUDENT))}
        self.assertNotIn("find_students", names)
        self.assertIn("search_documents", names)
        self.assertEqual(self.fx.registry.get("send_email").risk, RiskLevel.WRITE)
        self.assertEqual(self.fx.registry.get("update_student_record").risk, RiskLevel.HIGH_RISK)


if __name__ == "__main__":
    unittest.main()
