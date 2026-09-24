import asyncio
import io
import json
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock
from xml.etree import ElementTree

from app.actions.email import EmailService, OutgoingEmail, SmtpEmailSender
from app.actions.files import FORMAT_CONTENT_TYPES, render_csv, render_docx, render_pdf, render_pptx, render_report, render_table_pdf, render_xlsx
from app.api.platform_runtime import build_platform
from app.config.settings import AppSettings
from app.domain.principals import PrincipalType
from app.ingestion.parsers.excel_parser import parse_xlsx
from app.institution_data.store import InstitutionDataStore
from app.observability.tracing import TraceRecorder
from app.persistence.database import InMemoryControlStore
from app.policy.pdp import LocalPolicyDecisionPoint
from app.storage.object_store import InMemoryObjectStore
from app.workers.handlers import principal_from_snapshot
from app.workers.queue import InlineJobQueue, JobQueue, SqsJobQueue, ThreadJobQueue
from platform_fixtures import PlatformFixture, principal


class FakeSqsClient:
    """Minimal stand-in for boto3's SQS client: messages are delivered only when asked."""

    def __init__(self, *, fail_sends: bool = False) -> None:
        self.sent: list[dict] = []
        self.deleted: list[str] = []
        self.deliver: list[dict] = []
        self.fail_sends = fail_sends

    def send_message(self, QueueUrl, MessageBody):
        if self.fail_sends:
            raise RuntimeError("AccessDenied: sqs:SendMessage")
        self.sent.append(json.loads(MessageBody))

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds):
        batch, self.deliver = self.deliver[:MaxNumberOfMessages], self.deliver[MaxNumberOfMessages:]
        return {"Messages": [{"Body": json.dumps(item), "ReceiptHandle": f"rh-{index}"} for index, item in enumerate(batch)]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


def _age_running_job(store: InstitutionDataStore, job_id: str, seconds: float) -> None:
    """Make a running job look like its worker stopped reporting ``seconds`` ago."""

    started = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store.backend.transaction():
        store.backend.execute("UPDATE background_jobs SET started_at = ?, heartbeat_at = ? WHERE job_id = ?", (started, started, job_id))


async def _ok(payload):
    return {"ok": True, **payload}


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


    @staticmethod
    def _parts(package: bytes) -> dict[str, ElementTree.Element]:
        """Every XML part of an Office file, parsed: a malformed part fails the test."""

        archive = zipfile.ZipFile(io.BytesIO(package))
        self_check = archive.testzip()
        assert self_check is None, self_check
        return {name: ElementTree.fromstring(archive.read(name)) for name in archive.namelist() if name.endswith((".xml", ".rels"))}

    def test_word_document_holds_a_titled_table(self):
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        rows = [*self.rows, {"student_id": "MBA003", "name": "Bell\x07 <&> Kiran", "attendance_percent": 71}]
        parts = self._parts(render_docx("Low attendance", self.columns, rows, subtitle="MBA"))
        body = parts["word/document.xml"]
        texts = ["".join(node.text or "" for node in paragraph.iter(f"{w}t")) for paragraph in body.iter(f"{w}p")]
        self.assertEqual(texts[0], "Low attendance")
        self.assertTrue(texts[1].startswith("MBA · 3 rows · Generated"), texts[1])
        table_rows = list(body.iter(f"{w}tr"))
        self.assertEqual(len(table_rows), 4, "a header row and one row per record")
        self.assertIsNotNone(table_rows[0].find(f"{w}trPr/{w}tblHeader"), "the header repeats on every page")
        self.assertIn("Attendance Percent", texts)
        self.assertIn("Student ID", texts)
        self.assertIn("Bell <&> Kiran", texts, "control characters dropped, markup escaped")
        self.assertIn("docProps/core.xml", parts)
        wide = self._parts(render_docx("Wide", [f"c{i}" for i in range(9)], rows))["word/document.xml"]
        self.assertEqual(wide.find(f"{w}body/{w}sectPr/{w}pgSz").get(f"{w}orient"), "landscape")

    def test_word_document_says_when_it_leaves_rows_out(self):
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with mock.patch("app.actions.files.MAX_DOCX_ROWS", 2):
            body = self._parts(render_docx("Big", self.columns, [*self.rows, *self.rows]))["word/document.xml"]
        self.assertEqual(len(list(body.iter(f"{w}tr"))), 3)
        note = "".join(node.text or "" for node in list(body.iter(f"{w}p"))[1].iter(f"{w}t"))
        self.assertIn("Showing the first 2; ask for an Excel file to get every row.", note)

    def test_powerpoint_deck_pages_the_rows(self):
        a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
        rows = [{"student_id": f"MBA{i:03d}", "name": f"Student {i}", "attendance_percent": 60 + i % 15} for i in range(30)]
        parts = self._parts(render_pptx("Low attendance", self.columns, rows))
        slides = sorted(name for name in parts if name.startswith("ppt/slides/slide"))
        self.assertEqual(len(slides), 4, "a title slide and three slides of twelve rows")
        self.assertEqual(len(parts["ppt/presentation.xml"].findall(f"{p}sldIdLst/{p}sldId")), 4)
        overrides = {node.get("PartName") for node in parts["[Content_Types].xml"]}
        self.assertTrue({f"/{name}" for name in slides} <= overrides)
        table_rows = [len(list(parts[name].iter(f"{a}tr"))) for name in slides[1:]]
        self.assertEqual(table_rows, [13, 13, 7])
        headings = ["".join(node.text or "" for node in parts[name].iter(f"{a}t")) for name in slides[1:]]
        self.assertTrue(headings[2].startswith("Low attendance — rows 25–30 of 30"), headings[2])
        empty = self._parts(render_pptx("None", self.columns, []))
        self.assertEqual(len([name for name in empty if name.startswith("ppt/slides/slide")]), 2)

    def test_powerpoint_deck_keeps_to_what_fits(self):
        a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        columns = [f"c{i}" for i in range(14)]
        with mock.patch("app.actions.files.MAX_PPTX_ROWS", 5):
            parts = self._parts(render_pptx("Wide", columns, [{column: 1 for column in columns} for _ in range(9)]))
        self.assertEqual(len(parts["ppt/slides/slide2.xml"].findall(f".//{a}gridCol")), 10)
        about = "".join(node.text or "" for node in parts["ppt/slides/slide1.xml"].iter(f"{a}t"))
        self.assertIn("Showing the first 5", about)
        self.assertIn("Showing 10 of 14 columns.", about)

    def test_every_report_format_renders_with_its_content_type(self):
        for fmt in ("csv", "xlsx", "pdf", "docx", "pptx"):
            content, content_type = render_report(fmt, "Low attendance", self.columns, self.rows)
            self.assertTrue(content, fmt)
            self.assertEqual(content_type, FORMAT_CONTENT_TYPES[fmt], fmt)
        workbook = render_xlsx(self.columns, [{"student_id": "MBA009", "name": "Tab\x0bbed\x00", "attendance_percent": 1}])
        self.assertEqual(parse_xlsx("out.xlsx", workbook).tables[0].records[0].fields["name"], "Tabbed")


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
        self.assertEqual(seen, [{"n": 1, "_attempt": 1}], "handlers learn which attempt they are running")

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

    async def test_sqs_consumer_claims_only_the_job_named_by_the_message(self):
        client = FakeSqsClient()
        queue = SqsJobQueue(self.fx.store, "https://sqs.example.test/q", client=client)
        queue.register("t.ok", _ok)
        job_ids = [queue.enqueue("college_a", "t.ok", {"n": i}) for i in range(3)]
        self.assertEqual([item["job_id"] for item in client.sent], job_ids)
        client.deliver = [{"job_id": job_ids[1]}]
        self.assertEqual(await queue.consume_once(wait_seconds=0), 1)
        self.assertEqual(queue.status(job_ids[1])["status"], "succeeded")
        self.assertEqual([queue.status(job_ids[0])["status"], queue.status(job_ids[2])["status"]], ["queued", "queued"])
        self.assertEqual(client.deleted, ["rh-0"])
        # A redelivered or unknown message is acknowledged without touching other jobs.
        client.deliver = [{"job_id": job_ids[1]}, {"job_id": "bg-missing"}, {"nope": True}]
        self.assertEqual(await queue.consume_once(wait_seconds=0), 0)
        self.assertEqual([queue.status(job_ids[0])["status"], queue.status(job_ids[2])["status"]], ["queued", "queued"])
        self.assertEqual(len(client.deleted), 4)

    def test_sqs_publish_failure_marks_the_job_failed_instead_of_leaving_it_queued(self):
        queue = SqsJobQueue(self.fx.store, "https://sqs.example.test/q", client=FakeSqsClient(fail_sends=True))
        queue.register("t.ok", _ok)
        with self.assertRaisesRegex(RuntimeError, "could not be published to the job queue"):
            queue.enqueue("college_a", "t.ok", {})
        job = self.fx.store.list_background_jobs("college_a")[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("could not publish to SQS", job["error"])
        self.assertIn("AccessDenied", job["error"])

    def test_recover_stale_requeues_and_redispatches_interrupted_jobs(self):
        recorder = JobQueue(self.fx.store)  # records only; nothing dispatches
        recorder.register("t.ok", _ok)
        interrupted = recorder.enqueue("college_a", "t.ok", {"n": 1})
        fresh = recorder.enqueue("college_a", "t.ok", {"n": 2})
        exhausted = recorder.enqueue("college_a", "t.ok", {"n": 3})
        for job_id in (interrupted, fresh, exhausted):
            self.assertIsNotNone(self.fx.store.claim_background_job(job_id))
        _age_running_job(self.fx.store, interrupted, 3600)
        _age_running_job(self.fx.store, exhausted, 3600)
        with self.fx.store.backend.transaction():
            self.fx.store.backend.execute("UPDATE background_jobs SET attempts = 3 WHERE job_id = ?", (exhausted,))
        # Inline: the requeued job runs immediately, the young claim is left alone.
        inline = InlineJobQueue(self.fx.store)
        inline.register("t.ok", _ok)
        requeued = inline.recover_stale(older_than_seconds=900, max_attempts=3)
        self.assertEqual([job["job_id"] for job in requeued], [interrupted])
        self.assertEqual(inline.status(interrupted)["status"], "succeeded")
        self.assertEqual(inline.status(fresh)["status"], "running")
        self.assertEqual(inline.status(exhausted)["status"], "failed")
        # SQS: the message must be published again, since the worker only wakes on messages.
        stale_sqs = recorder.enqueue("college_a", "t.ok", {"n": 4})
        self.fx.store.claim_background_job(stale_sqs)
        _age_running_job(self.fx.store, stale_sqs, 3600)
        client = FakeSqsClient()
        sqs = SqsJobQueue(self.fx.store, "https://sqs.example.test/q", client=client)
        sqs.register("t.ok", _ok)
        self.assertEqual([job["job_id"] for job in sqs.recover_stale(older_than_seconds=900)], [stale_sqs])
        self.assertEqual(client.sent, [{"job_id": stale_sqs}])
        self.assertEqual(sqs.status(stale_sqs)["status"], "queued")
        # A re-dispatch that fails is recorded on the job instead of raising out of boot.
        broken = recorder.enqueue("college_a", "t.ok", {"n": 5})
        self.fx.store.claim_background_job(broken)
        _age_running_job(self.fx.store, broken, 3600)
        failing = SqsJobQueue(self.fx.store, "https://sqs.example.test/q", client=FakeSqsClient(fail_sends=True))
        failing.register("t.ok", _ok)
        failing.recover_stale(older_than_seconds=900)
        self.assertEqual(failing.status(broken)["status"], "failed")

    async def test_thread_queue_start_is_idempotent_and_wakes_for_recovered_jobs(self):
        queue = ThreadJobQueue(self.fx.store, poll_seconds=0.05)
        queue.register("t.ok", _ok)
        job_id = queue.enqueue("college_a", "t.ok", {})
        queue.stop()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and queue.status(job_id)["status"] != "succeeded":
            await asyncio.sleep(0.05)
        # Leave a claim behind the way a killed process would, then boot again.
        stale = JobQueue(self.fx.store)
        stale.register("t.ok", _ok)
        interrupted = stale.enqueue("college_a", "t.ok", {"n": 9})
        self.fx.store.claim_background_job(interrupted)
        _age_running_job(self.fx.store, interrupted, 3600)
        queue.start()
        queue.start()
        first_thread = queue._thread
        self.assertTrue(first_thread.is_alive())
        queue.recover_stale(older_than_seconds=900)
        self.assertIs(queue._thread, first_thread)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and queue.status(interrupted)["status"] != "succeeded":
            await asyncio.sleep(0.05)
        queue.stop()
        self.assertEqual(queue.status(interrupted)["status"], "succeeded")

    async def test_build_platform_starts_the_thread_queue_and_resumes_interrupted_jobs(self):
        store = InstitutionDataStore(":memory:")
        store.upsert_institution("college_a", "ABC College")
        pending = JobQueue(store)
        pending.register("intelligence.monitor", _ok)
        interrupted = pending.enqueue("college_a", "intelligence.monitor", {"institution_id": "college_a"})
        store.claim_background_job(interrupted)
        _age_running_job(store, interrupted, 3600)
        young = pending.enqueue("college_a", "intelligence.monitor", {"institution_id": "college_a"})
        store.claim_background_job(young)
        settings = AppSettings(control_database_url=":memory:", object_store_backend="memory", job_queue="thread", job_stale_seconds=60)
        # A process that does not run workers (scripts, one-off commands) neither polls nor sweeps.
        passive = build_platform(settings, control_store=InMemoryControlStore(), pdp=LocalPolicyDecisionPoint(), tracer=TraceRecorder(), model=None, institution_store=store, objects=InMemoryObjectStore())
        self.assertIsNone(passive.jobs._thread, "only processes meant to run jobs start the polling thread")
        self.assertEqual(passive.jobs.status(interrupted)["status"], "running", "a passive process never touches jobs")
        runtime = build_platform(settings, control_store=InMemoryControlStore(), pdp=LocalPolicyDecisionPoint(), tracer=TraceRecorder(), model=None, institution_store=store, objects=InMemoryObjectStore(), start_workers=True)
        try:
            self.assertIsInstance(runtime.jobs, ThreadJobQueue)
            self.assertTrue(runtime.jobs._thread is not None and runtime.jobs._thread.is_alive(), "thread queue must start at boot, not on first enqueue")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and runtime.jobs.status(interrupted)["status"] in {"queued", "running"}:
                await asyncio.sleep(0.05)
            # No monitor is configured, so the handler is absent and the job finishes as failed;
            # what matters is that the stale claim was picked up again at boot.
            self.assertEqual(runtime.jobs.status(interrupted)["status"], "failed")
            self.assertEqual(runtime.jobs.status(interrupted)["error"], "no handler registered")
            self.assertEqual(runtime.jobs.status(young)["status"], "running")
        finally:
            runtime.close()

    async def test_heartbeats_keep_a_live_job_from_being_requeued(self):
        store = self.fx.store
        queue = JobQueue(store, stale_seconds=0.3, heartbeat_seconds=0.05)
        release = threading.Event()

        async def slow(payload):
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
            return {"attempt": payload["_attempt"]}

        queue.register("t.slow", slow)
        job_id = queue.enqueue("college_a", "t.slow", {})
        job = store.claim_background_job(job_id)
        first_beat = job["heartbeat_at"]
        task = asyncio.create_task(queue.run_job(job))
        await asyncio.sleep(0.6)
        self.assertEqual(queue.recover_stale(), [], "a job whose worker heartbeats is never handed back")
        self.assertEqual(store.get_background_job(job_id)["status"], "running")
        self.assertGreater(store.get_background_job(job_id)["heartbeat_at"], first_beat)
        release.set()
        await task
        self.assertEqual(store.get_background_job(job_id)["status"], "succeeded")
        self.assertEqual(store.get_background_job(job_id)["result"]["attempt"], 1)
        # A job whose heartbeat stopped is handed back within the stale window, and the re-run is attempt 2.
        stale = queue.enqueue("college_a", "t.slow", {})
        store.claim_background_job(stale)
        _age_running_job(store, stale, 1.0)
        self.assertEqual([item["job_id"] for item in queue.recover_stale()], [stale])
        self.assertEqual(store.get_background_job(stale)["status"], "queued")
        release.clear()
        job = store.claim_background_job(stale)
        self.assertEqual(job["attempts"], 2)
        release.set()
        await queue.run_job(job)
        self.assertEqual(store.get_background_job(stale)["result"]["attempt"], 2)

    async def test_stopping_the_thread_queue_hands_back_the_job_in_flight(self):
        store = self.fx.store
        queue = ThreadJobQueue(store, poll_seconds=0.05, heartbeat_seconds=0.05)
        release = threading.Event()

        async def slow(payload):
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 10)
            return {"ok": True}

        queue.register("t.slow", slow)
        job_id = queue.enqueue("college_a", "t.slow", {})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and queue.status(job_id)["status"] != "running":
            await asyncio.sleep(0.02)
        self.assertEqual(queue.status(job_id)["status"], "running")
        queue.stop(timeout=0.2)
        self.assertEqual(queue.status(job_id)["status"], "queued", "an unfinished job goes straight back to the queue on shutdown")
        self.assertIsNone(queue.status(job_id)["started_at"])
        # The abandoned run finishing later cannot overwrite a job that was handed back and re-claimed.
        store.claim_background_job(job_id)
        release.set()
        await asyncio.sleep(0.3)
        self.assertEqual(queue.status(job_id)["status"], "running")

    def test_abandoned_ingestion_jobs_are_marked_failed_through_the_failure_hook(self):
        from app.workers.handlers import register_handlers

        client = FakeSqsClient()
        queue = SqsJobQueue(self.fx.store, "https://sqs.example.com/q", client=client)
        register_handlers(queue, ingestion=self.fx.ingestion)
        job = self.fx.ingestion.upload("college_a", "staff-1", file_name="s.csv", content=b"Name,USN\nRavi,MBA001\n", content_type="text/csv")
        background = queue.enqueue("college_a", "ingestion.process", {"institution_id": "college_a", "job_id": job["job_id"], "requested_by": "staff-1"})
        self.fx.store.claim_background_job(background)
        _age_running_job(self.fx.store, background, 3600)
        client.fail_sends = True
        queue.recover_stale()
        self.assertEqual(self.fx.store.get_background_job(background)["status"], "failed")
        ingestion_job = self.fx.store.get_job("college_a", job["job_id"])
        self.assertEqual(ingestion_job["status"], "failed")
        self.assertIn("could not be scheduled", ingestion_job["error"])

    def test_principal_snapshot_round_trip(self):
        original = principal(PrincipalType.HOD)
        rebuilt = principal_from_snapshot({"principal_id": original.principal_id, "principal_type": "hod", "capabilities": sorted(c.value for c in original.capabilities), "scopes": [s.as_dict() for s in original.scopes], "consent_verified": False})
        self.assertEqual(rebuilt.capabilities, original.capabilities)
        self.assertEqual(rebuilt.scopes, original.scopes)


if __name__ == "__main__":
    unittest.main()
