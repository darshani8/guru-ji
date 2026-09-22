import asyncio
import io
import time
import unittest
import zipfile

from app.actions.email import EmailService, OutgoingEmail, SmtpEmailSender
from app.actions.files import render_csv, render_pdf, render_table_pdf, render_xlsx
from app.domain.principals import PrincipalType
from app.ingestion.parsers.excel_parser import parse_xlsx
from app.workers.handlers import principal_from_snapshot
from app.workers.queue import InlineJobQueue, ThreadJobQueue
from platform_fixtures import PlatformFixture, principal


class FileRendererTests(unittest.TestCase):
    columns = ["student_id", "name", "attendance_percent"]
    rows = [{"student_id": "MBA001", "name": "Ravi (Kumar)", "attendance_percent": 70.0}, {"student_id": "MBA002", "name": "Asha", "attendance_percent": 65.5}]

    def test_csv_and_xlsx_round_trip_through_the_ingestion_reader(self):
        self.assertTrue(render_csv(self.columns, self.rows).startswith("﻿student_id".encode()))
        workbook = render_xlsx(self.columns, self.rows, sheet_name="Low attendance")
        self.assertEqual(zipfile.ZipFile(io.BytesIO(workbook)).testzip(), None)
        parsed = parse_xlsx("out.xlsx", workbook)
        self.assertEqual(parsed.tables[0].headers, tuple(self.columns))
        self.assertEqual(parsed.tables[0].records[1].fields["attendance_percent"], 65.5)

    def test_pdf_structure_is_valid(self):
        pdf = render_table_pdf("Low attendance", self.columns, self.rows, subtitle="MBA")
        self.assertTrue(pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"))
        xref_offset = int(pdf[pdf.rfind(b"startxref") + 9:].split()[0])
        self.assertTrue(pdf[xref_offset:].startswith(b"xref"))
        for line in pdf[xref_offset:].split(b"\n")[2:8]:
            if line.endswith(b" n "):
                offset = int(line.split()[0])
                self.assertRegex(pdf[offset:offset + 12].decode("latin-1"), r"^\d+ 0 obj")
        multipage = render_pdf("Big", [f"line {i}" for i in range(200)])
        self.assertEqual(multipage.count(b"/Type /Page "), 4)


class EmailAndQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture()

    def test_email_recipients_resolve_from_directory_or_allowed_domains_only(self):
        service: EmailService = self.fx.email
        resolved, unresolved = service.resolve_recipients("college_a", ["F01", "Dr Prakash", "someone@abc.edu.in", "leak@gmail.com", "Nobody"])
        self.assertEqual([item["email"] for item in resolved], ["meena@abc.edu.in", "principal@abc.edu.in", "someone@abc.edu.in"])
        self.assertEqual(unresolved, ["leak@gmail.com", "Nobody"])
        with self.assertRaises(ValueError):
            service.send(principal(PrincipalType.HOD), "college_a", recipients=["leak@gmail.com"], subject="s", body="b")
        with self.assertRaises(PermissionError):
            service.send(principal(PrincipalType.FACULTY), "college_a", recipients=["F01"], subject="s", body="b")

    def test_smtp_failures_are_reported_not_raised(self):
        sender = SmtpEmailSender(host="127.0.0.1", port=1, sender="a@b.c", timeout_seconds=0.2)
        delivery = sender.send(OutgoingEmail(to=("x@abc.edu.in",), subject="s", body="b"))
        self.assertEqual(delivery.status, "failed")
        self.assertTrue(delivery.error)

    async def test_inline_queue_runs_handlers_and_records_failures(self):
        queue = InlineJobQueue(self.fx.store)
        seen = []

        async def ok(payload):
            seen.append(payload)
            return {"n": payload["n"]}

        async def bad(payload):
            raise ValueError("boom")

        queue.register("t.ok", ok)
        queue.register("t.bad", bad)
        job_id = queue.enqueue("college_a", "t.ok", {"n": 1})
        self.assertEqual(queue.status(job_id)["status"], "succeeded")
        self.assertEqual(queue.status(queue.enqueue("college_a", "t.bad", {}))["error"], "boom")
        with self.assertRaises(ValueError):
            queue.enqueue("college_a", "t.unknown", {})
        self.assertEqual(seen, [{"n": 1}])

    async def test_thread_queue_processes_in_background(self):
        queue = ThreadJobQueue(self.fx.store, poll_seconds=0.05)
        done = asyncio.Event()

        async def handler(payload):
            return {"ok": True}

        queue.register("t.ok", handler)
        job_id = queue.enqueue("college_a", "t.ok", {})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and queue.status(job_id)["status"] != "succeeded":
            await asyncio.sleep(0.05)
        queue.stop()
        self.assertEqual(queue.status(job_id)["status"], "succeeded")
        del done

    async def test_ingestion_handler_notifies_requester(self):
        job = self.fx.ingestion.upload("college_a", "staff-1", file_name="s.csv", content=b"Student Name,USN\nA,X1\n", content_type="text/csv")
        self.fx.jobs.enqueue("college_a", "ingestion.process", {"institution_id": "college_a", "job_id": job["job_id"], "requested_by": "staff-1"})
        self.assertEqual(self.fx.store.get_job("college_a", job["job_id"])["status"], "imported")
        inbox = self.fx.store.list_notifications("college_a", "staff-1")
        self.assertIn("Inserted 1", inbox[0]["body"])

    def test_principal_snapshot_round_trip(self):
        original = principal(PrincipalType.HOD)
        rebuilt = principal_from_snapshot({"principal_id": original.principal_id, "principal_type": "hod", "capabilities": sorted(c.value for c in original.capabilities), "scopes": [s.as_dict() for s in original.scopes], "consent_verified": False})
        self.assertEqual(rebuilt.capabilities, original.capabilities)
        self.assertEqual(rebuilt.scopes, original.scopes)


if __name__ == "__main__":
    unittest.main()
