import time
import unittest
from datetime import datetime, timedelta, timezone

from app.ingestion.registry import ParserRegistry
from app.ingestion.service import (
    JOB_FAILED,
    JOB_IMPORTED,
    JOB_NEEDS_REVIEW,
    JOB_PROCESSING,
    JOB_READY,
    STAGE_DUPLICATE_REVIEW,
    STAGE_MAPPING_REVIEW,
    STAGE_NORMALIZING,
    STAGE_READY,
    CommitError,
    IngestionError,
    IngestionService,
)
from app.institution_data.store import InstitutionDataStore
from app.storage.object_store import InMemoryObjectStore

STUDENTS = b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\nRAVI KUMAR,1MS23MBA001,M.B.A,Sem 1,98765 43210,Ravi@X.com,12/05/2003\nAsha Rao,1MS23MBA002,MBA,1,9876543211,asha@x.com,2003-01-15\nAsha Rao,1MS23MBA002,MBA,1,9876543211,asha@x.com,2003-01-15\n"


class IngestionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = InstitutionDataStore(":memory:")
        self.objects = InMemoryObjectStore()
        self.service = IngestionService(store=self.store, objects=self.objects, parsers=ParserRegistry())

    async def _upload(self, name: str, content: bytes, **kwargs):
        job = self.service.upload("college_a", "staff-1", file_name=name, content=content, content_type="text/csv", **kwargs)
        return await self.service.process("college_a", job["job_id"])

    async def test_confident_mapping_auto_commits_with_cleaning_dedup_and_lineage(self):
        job = await self._upload("students.csv", STUDENTS)
        self.assertEqual(job["status"], JOB_IMPORTED)
        report = job["report"]
        self.assertEqual(report["import"]["inserted"], 2)
        self.assertEqual(report["import"]["skipped"], 1)
        self.assertEqual(report["normalization"]["planned_actions"], {"insert": 2, "duplicate_in_batch": 1})
        self.assertIn("program_alias", report["normalization"]["normalizations_applied"])
        record = self.store.get_record("college_a", "student", "1ms23mba001")
        self.assertEqual(record["name"], "Ravi Kumar")
        self.assertEqual(record["program"], "MBA")
        self.assertEqual(record["phone"], "9876543210")
        self.assertEqual(record["date_of_birth"], "2003-05-12")
        self.assertEqual(record["lineage"]["source_file_name"], "students.csv")
        self.assertEqual(record["lineage"]["source_locator"], "csv;row=2")
        self.assertTrue(self.objects.exists(self.store.get_file("college_a", job["file_id"])["object_key"]))
        self.assertEqual(self.service.import_report("college_a", job["job_id"])["pending_reviews"], 0)

    async def test_uncertain_mapping_waits_for_review_and_remembers_the_decision(self):
        content = b"Name,ID,Contact,Prog,Semester,Remarks\nRavi Kumar,1MS23MBA001,9876543210,MBA,2,fine\nNew Person,1MS23MBA009,9876543299,MBA,2,-\n"
        job = await self._upload("list.csv", content)
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        self.assertIn("Prog", job["mapping"]["review_required"])
        with self.assertRaises(IngestionError):
            self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        mapping = dict(review["payload"]["proposed_mapping"])
        mapping["Prog"] = "program"
        with self.assertRaises(IngestionError):
            await self.service.apply_mapping("college_a", job["job_id"], mapping={"Name": "name"}, entity="student", approved_by="staff-1")
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="student", approved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 2)
        again = await self._upload("list2.csv", content)
        self.assertEqual(again["status"], JOB_IMPORTED)
        self.assertTrue(again["mapping"]["profile_applied"])
        self.assertEqual(again["report"]["import"]["unchanged"], 2)

    async def test_probable_duplicate_person_goes_to_review_with_both_outcomes(self):
        await self._upload("students.csv", STUDENTS)
        same_person = b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\nRavi Kumar,1MS23MBA777,MBA,1,9876543210,ravi@x.com,12/05/2003\n"
        job = await self._upload("students3.csv", same_person)
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_DUPLICATE_REVIEW))
        candidate = job["report"]["normalization"]["duplicate_candidates"][0]
        self.assertEqual(candidate["kind"], "probable_person")
        self.assertEqual(candidate["evidence"]["existing_record_key"], "1ms23mba001")
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        job = self.service.resolve_review("college_a", review["review_id"], decision="approved", resolved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 0)
        job = await self._upload("students4.csv", same_person.replace(b"777", b"778"))
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        job = self.service.resolve_review("college_a", review["review_id"], decision="rejected", resolved_by="staff-1")
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        self.assertEqual(self.store.count_records("college_a", "student"), 3)

    async def test_approving_a_duplicate_keeps_the_update_and_drops_the_new_identity(self):
        await self._upload("students.csv", STUDENTS)
        # Row 2 updates the existing student (new semester); row 3 is the same person under a new USN.
        roster = (
            b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\n"
            b"Ravi Kumar,1MS23MBA001,MBA,2,9876543210,ravi@x.com,12/05/2003\n"
            b"Ravi Kumar,1MS23MBA901,MBA,2,9876543210,ravi@x.com,12/05/2003\n"
        )
        job = await self._upload("roster2.csv", roster)
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_DUPLICATE_REVIEW))
        items = self.store.list_review_items("college_a", job_id=job["job_id"])
        self.assertGreaterEqual(len(items), 1)
        for item in items:
            job = self.service.resolve_review("college_a", item["review_id"], decision="approved", resolved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["updated"], 1, "the existing student's update is imported")
        self.assertEqual(job["report"]["import"]["inserted"], 0, "the new identity is not imported")
        self.assertEqual(self.store.get_record("college_a", "student", "1ms23mba001")["semester"], 2)
        self.assertIsNone(self.store.get_record("college_a", "student", "1ms23mba901"))
        self.assertEqual(self.store.count_records("college_a", "student"), 2)
        # Two brand-new rows for one person keep the first occurrence.
        fresh = (
            b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\n"
            b"Meena Nair,1MS23MBA501,MBA,1,9876500501,meena@x.com,03/03/2003\n"
            b"Meena Nair,1MS23MBA502,MBA,1,9876500501,meena@x.com,03/03/2003\n"
        )
        job = await self._upload("roster3.csv", fresh)
        for item in self.store.list_review_items("college_a", job_id=job["job_id"]):
            job = self.service.resolve_review("college_a", item["review_id"], decision="approved", resolved_by="staff-1")
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        self.assertIsNotNone(self.store.get_record("college_a", "student", "1ms23mba501"))
        self.assertIsNone(self.store.get_record("college_a", "student", "1ms23mba502"))

    async def test_updates_are_detected_when_data_changes(self):
        await self._upload("students.csv", STUDENTS)
        job = await self._upload("students_new.csv", STUDENTS.replace(b"Sem 1", b"Sem 2"))
        self.assertEqual((job["report"]["import"]["updated"], job["report"]["import"]["unchanged"]), (1, 1))
        self.assertEqual(self.store.get_record("college_a", "student", "1ms23mba001")["semester"], 2)

    async def test_manual_commit_when_auto_commit_is_off_and_failures_are_explicit(self):
        job = await self._upload("students.csv", STUDENTS, options={"auto_commit": False})
        self.assertEqual(job["status"], "ready")
        job = self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        failed = await self._upload("notes.txt", b"just some prose without a table")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("no tabular rows", failed["error"])
        with self.assertRaises(IngestionError):
            self.service.upload("college_a", "staff-1", file_name="x.csv", content=b"", content_type="text/csv")
        with self.assertRaises(IngestionError):
            self.service.upload("college_a", "staff-1", file_name="x.csv", content=b"a,b", content_type="text/csv", entity_hint="unicorn")

    async def test_blocking_row_issues_keep_other_rows(self):
        content = b"USN,Subject Code,Total Classes,Attended,Month\n1MS23MBA001,MBA101,20,25,Aug\n1MS23MBA002,MBA101,20,19,Aug\n"
        job = await self._upload("attendance.csv", content)
        self.assertEqual(job["entity"], "attendance")
        self.assertEqual(job["report"]["normalization"]["rows_rejected"], 1)
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        rejected = self.store.job_records("college_a", job["job_id"], status="rejected")[0]
        self.assertIn("attended_exceeds_held", [item["code"] for item in rejected["issues"]])

    async def test_parser_and_unexpected_failures_mark_the_job_failed_not_stuck(self):
        job = await self._upload("bad.csv", b"a,b\n\"" + b"x" * 200_000 + b"\n1,2\n")
        self.assertEqual(job["status"], JOB_FAILED)
        self.assertIn("CSV could not be parsed", job["error"])
        original = self.service.parsers

        class _ExplodingRegistry(ParserRegistry):
            def parse(self, *args, **kwargs):
                raise RuntimeError("secret internal detail")

        self.service.parsers = _ExplodingRegistry()
        try:
            with self.assertLogs("app.ingestion.service", level="ERROR") as logs:
                job = await self._upload("students.csv", STUDENTS)
        finally:
            self.service.parsers = original
        self.assertEqual(job["status"], JOB_FAILED)
        self.assertIn("RuntimeError", job["error"])
        self.assertNotIn("secret internal detail", job["error"])
        self.assertTrue(any("failed unexpectedly" in line for line in logs.output))
        # A failed job can be processed again once the cause is fixed.
        job = await self.service.process("college_a", job["job_id"])
        self.assertEqual(job["status"], JOB_IMPORTED)

    async def test_commit_failure_returns_the_job_to_ready_with_the_error(self):
        job = await self._upload("students.csv", STUDENTS, options={"auto_commit": False})
        original = self.store.upsert_records

        def broken(*args, **kwargs):
            raise RuntimeError("integer out of range")

        self.store.upsert_records = broken
        try:
            with self.assertLogs("app.ingestion.service", level="ERROR"):
                with self.assertRaises(CommitError):
                    self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        finally:
            self.store.upsert_records = original
        job = self.store.get_job("college_a", job["job_id"])
        self.assertEqual((job["status"], job["stage"]), (JOB_READY, STAGE_READY))
        self.assertIn("RuntimeError", job["error"])
        job = self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertIsNone(job["error"])
        # Auto-commit inside process() leaves the job ready too, never failed.
        self.store.upsert_records = broken
        try:
            with self.assertLogs("app.ingestion.service", level="ERROR"):
                job = await self._upload("students2.csv", b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\nKiran Rao,1MS23MBA050,MBA,1,9876500000,kiran@x.com,2003-02-02\n")
        finally:
            self.store.upsert_records = original
        self.assertEqual((job["status"], job["stage"]), (JOB_READY, STAGE_READY))

    async def test_interrupted_processing_job_is_restarted_from_scratch(self):
        job = self.service.upload("college_a", "staff-1", file_name="students.csv", content=STUDENTS, content_type="text/csv")
        # Simulate a worker restart mid-run: status processing, stale rows and a stale review item.
        self.store.update_job("college_a", job["job_id"], status=JOB_PROCESSING, stage=STAGE_NORMALIZING, entity="student", mapping={"stale": True})
        self.store.replace_job_records("college_a", job["job_id"], [{"row_number": 99, "locator": "csv;row=99", "raw": {"x": 1}, "normalized": {}, "status": "ready", "action": "insert", "issues": []}])
        self.store.add_review_items("college_a", job["job_id"], [{"kind": "duplicate", "payload": {"left_locator": "csv;row=99"}}])
        # A run whose heartbeat is fresh is still alive: it is left alone, nothing is reset.
        untouched = await self.service.process("college_a", job["job_id"])
        self.assertEqual((untouched["status"], untouched["stage"]), (JOB_PROCESSING, STAGE_NORMALIZING))
        self.assertEqual(len(self.store.list_review_items("college_a", job_id=job["job_id"], status="pending")), 1)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=self.service.restart_after_seconds + 60)).isoformat()
        self.store.backend.execute("UPDATE ingestion_jobs SET updated_at = ? WHERE job_id = ?", (stale, job["job_id"]))
        job = await self.service.process("college_a", job["job_id"])
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 2)
        self.assertEqual(job["report"]["restarted"]["from_stage"], STAGE_NORMALIZING)
        self.assertNotIn("stale", job["mapping"])
        self.assertEqual(self.store.list_review_items("college_a", job_id=job["job_id"], status="pending"), [])
        self.assertNotIn("csv;row=99", [row["locator"] for row in self.store.job_records("college_a", job["job_id"])])
        # Finished jobs are left alone.
        self.assertEqual((await self.service.process("college_a", job["job_id"]))["report"]["import"]["inserted"], 2)

    async def test_route_run_normalization_failure_marks_the_job_failed_and_is_retryable(self):
        from unittest import mock

        from app.ingestion.service import JOB_FAILED, ProcessingError

        content = b"Name,ID,Contact,Prog,Semester,Remarks\nRavi Kumar,1MS23MBA001,9876543210,MBA,2,fine\n"
        job = await self._upload("list.csv", content)
        self.assertEqual(job["stage"], STAGE_MAPPING_REVIEW)
        mapping = dict(self.store.list_review_items("college_a", job_id=job["job_id"])[0]["payload"]["proposed_mapping"])
        mapping["Prog"] = "program"
        with mock.patch.object(type(self.store), "lookup_person_matches", side_effect=RuntimeError("connection reset")), self.assertLogs("app.ingestion.service", level="ERROR"):
            with self.assertRaises(ProcessingError) as raised:
                await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="student", approved_by="staff-1")
        self.assertNotIn("connection reset", str(raised.exception))
        failed = self.store.get_job("college_a", job["job_id"])
        self.assertEqual((failed["status"], failed["stage"]), (JOB_FAILED, STAGE_NORMALIZING))
        self.assertIn("RuntimeError", failed["error"])
        # Submitting the mapping again resumes the job once the cause is fixed.
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="student", approved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)

    async def test_saved_profile_applies_through_normalised_headers_and_never_skips_review_when_incomplete(self):
        content = b"Name,ID,Contact,Prog,Semester,Remarks\nRavi Kumar,1MS23MBA001,9876543210,MBA,2,fine\n"
        job = await self._upload("list.csv", content)
        self.assertEqual(job["stage"], STAGE_MAPPING_REVIEW)
        mapping = dict(self.store.list_review_items("college_a", job_id=job["job_id"])[0]["payload"]["proposed_mapping"])
        mapping["Prog"] = "program"
        await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="student", approved_by="staff-1")
        # Same columns, different case and spacing: the profile still applies.
        again = await self._upload("list2.csv", b"NAME,id,CONTACT,PROG,SEMESTER,REMARKS\nNew Person,1MS23MBA009,9876543299,MBA,2,-\n")
        self.assertEqual(again["status"], JOB_IMPORTED)
        self.assertTrue(again["mapping"]["profile_applied"])
        self.assertEqual(again["report"]["import"]["inserted"], 1)
        self.assertIsNotNone(self.store.get_record("college_a", "student", "1ms23mba009"))
        # A profile that no longer covers the required fields falls back to review instead of importing nothing.
        headers = ["Name", "ID", "Contact", "Prog", "Semester", "Remarks"]
        from app.normalization.mapping import header_signature

        self.store.save_mapping_profile("college_a", entity="student", header_signature=header_signature(headers), mapping={"Remarks": "program"}, approved_by="staff-1")
        stale = await self._upload("list3.csv", content.replace(b"1MS23MBA001", b"1MS23MBA011"))
        self.assertEqual((stale["status"], stale["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        self.assertTrue(stale["mapping"]["profile_incomplete"])
        self.assertFalse(stale["mapping"]["profile_applied"])
        review = self.store.list_review_items("college_a", job_id=stale["job_id"])[0]
        self.assertEqual(review["kind"], "mapping")
        self.assertEqual(review["payload"]["proposed_mapping"]["Name"], "name")

    async def test_mapping_review_items_cannot_be_resolved_or_committed_as_duplicates(self):
        content = b"Name,ID,Contact,Prog,Semester,Remarks\nRavi Kumar,1MS23MBA001,9876543210,MBA,2,fine\n"
        job = await self._upload("list.csv", content)
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        self.assertEqual(review["kind"], "mapping")
        with self.assertRaises(IngestionError):
            self.service.resolve_review("college_a", review["review_id"], decision="approved", resolved_by="staff-1")
        self.assertEqual(self.store.list_review_items("college_a", job_id=job["job_id"])[0]["status"], "pending")
        with self.assertRaises(IngestionError):
            self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        with self.assertRaises(KeyError):
            self.service.resolve_review("college_a", "review-missing", decision="approved", resolved_by="staff-1")
        job = self.store.get_job("college_a", job["job_id"])
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))

    async def test_commit_is_refused_while_duplicate_reviews_are_pending(self):
        await self._upload("students.csv", STUDENTS)
        same_person = b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\nRavi Kumar,1MS23MBA777,MBA,1,9876543210,ravi@x.com,12/05/2003\n"
        job = await self._upload("students3.csv", same_person)
        self.assertEqual(job["stage"], STAGE_DUPLICATE_REVIEW)
        with self.assertRaises(IngestionError) as raised:
            self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        self.assertIn("duplicate review", str(raised.exception))
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        job = self.service.resolve_review("college_a", review["review_id"], decision="rejected", resolved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        self.assertEqual(self.store.count_records("college_a", "student"), 3)

    async def test_large_roster_of_distinct_students_dedupes_quickly(self):
        lines = [b"Student Name,USN,Course,Sem,Phone,Email ID,DOB"]
        for i in range(4000):
            lines.append(f"Person{i} Surname{i},1MS23MBA{i:05d},MBA,1,9{i:09d},p{i}@x.com,{1980 + i // 336}-{1 + (i // 28) % 12:02d}-{1 + i % 28:02d}".encode())
        started = time.perf_counter()
        job = await self._upload("big.csv", b"\n".join(lines) + b"\n")
        elapsed = time.perf_counter() - started
        self.assertEqual(job["status"], JOB_IMPORTED, job.get("error"))
        self.assertEqual(job["report"]["import"]["inserted"], 4000)
        self.assertEqual(job["report"]["normalization"]["duplicate_candidates"], [])
        self.assertLess(elapsed, 5.0, f"4000 distinct students took {elapsed:.1f}s")



class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    """What a reviewer approves is what gets imported."""

    def setUp(self):
        self.store = InstitutionDataStore(":memory:")
        self.objects = InMemoryObjectStore()
        self.service = IngestionService(store=self.store, objects=self.objects, parsers=ParserRegistry())

    async def _upload(self, name: str, content: bytes, **kwargs):
        job = self.service.upload("college_a", "staff-1", file_name=name, content=content, content_type="text/csv", **kwargs)
        return await self.service.process("college_a", job["job_id"])

    def _mapping_review(self, job):
        items = [item for item in self.store.list_review_items("college_a", job_id=job["job_id"]) if item["kind"] == "mapping"]
        self.assertEqual(len(items), 1)
        return items[0]["payload"]

    async def _decide_duplicates(self, job, decision):
        for item in self.store.list_review_items("college_a", job_id=job["job_id"]):
            job = self.service.resolve_review("college_a", item["review_id"], decision=decision, resolved_by="staff-1")
        return job

    async def test_similar_columns_are_proposed_so_approving_as_shown_imports(self):
        content = (
            b"Name,USN,Email,Email ID,Father Name,Mother Name\n"
            b"Ravi Kumar,1MS23MBA001,ravi@x.com,ravi.k@x.com,Suresh,Lata\n"
            b"Asha Rao,1MS23MBA002,asha@x.com,asha.r@x.com,Mohan,Gita\n"
        )
        job = await self._upload("parents.csv", content)
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        payload = self._mapping_review(job)
        proposed = [target for target in payload["proposed_mapping"].values() if target]
        self.assertEqual(len(proposed), len(set(proposed)), payload["proposed_mapping"])
        self.assertTrue({"Email ID", "Mother Name"} <= {item["source_header"] for item in payload["review_required"]})
        # Exactly what the console sends when the reviewer changes nothing.
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=payload["proposed_mapping"], entity=payload["entity"], approved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 2)
        record = self.store.get_record("college_a", "student", "1ms23mba001")
        self.assertEqual(record["email"], "ravi@x.com")
        self.assertEqual(record["attributes"]["Email ID"], "ravi.k@x.com")

    async def test_two_columns_on_one_field_are_named_in_the_refusal(self):
        job = await self._upload("parents.csv", b"Name,USN,Email,Email ID\nRavi Kumar,1MS23MBA001,ravi@x.com,ravi.k@x.com\n")
        with self.assertRaises(IngestionError) as raised:
            await self.service.apply_mapping("college_a", job["job_id"], mapping={"Name": "name", "USN": "student_id", "Email": "email", "Email ID": "email"}, entity="student", approved_by="staff-1")
        self.assertIn("'Email' and 'Email ID'", str(raised.exception))
        self.assertEqual(self.store.get_job("college_a", job["job_id"])["status"], JOB_NEEDS_REVIEW)

    async def test_a_doubtful_entity_is_reviewed_with_its_alternatives_and_a_correction_is_remembered(self):
        content = b"USN,Program,Semester,Academic Year,Section\n1MS23MBA001,MBA,1,2025-26,A\n1MS23MBA002,MBA,1,2025-26,B\n"
        job = await self._upload("enrollment.csv", content)
        self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        payload = self._mapping_review(job)
        self.assertTrue(payload["entity_uncertain"])
        self.assertLess(payload["entity_confidence"], 0.8)
        self.assertIn("enrollment", [item["entity"] for item in payload["entity_alternatives"]])
        # The reviewer picks the right kind of records; the job imports under it.
        mapping = {"USN": "student_id", "Program": "program", "Semester": "semester", "Academic Year": "academic_year", "Section": "section"}
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="enrollment", approved_by="staff-1")
        self.assertEqual((job["status"], job["entity"]), (JOB_IMPORTED, "enrollment"))
        self.assertEqual(job["report"]["import"]["inserted"], 2)
        # The same headers again, with nothing chosen at upload: the correction is reused.
        again = await self._upload("enrollment2.csv", content)
        self.assertEqual((again["status"], again["entity"]), (JOB_IMPORTED, "enrollment"))
        self.assertTrue(again["mapping"]["profile_applied"])
        # A confident detection or the uploader's own choice is not questioned.
        chosen = await self._upload("enrollment3.csv", content.replace(b"MBA00", b"MBA10"), entity_hint="enrollment")
        self.assertFalse(chosen["mapping"]["entity_uncertain"])
        confident = await self._upload("students.csv", STUDENTS)
        self.assertEqual(confident["status"], JOB_IMPORTED)

    async def test_an_approval_that_imports_nothing_fails_with_the_reason_and_is_not_remembered(self):
        content = b"USN,Student Name,Subject Code,Exam,Total Marks,Max Marks\n1MS23MBA001,Ravi,MBA101,IA1,42,25\n1MS23MBA002,Asha,MBA101,IA1,40,25\n"
        job = await self._upload("ia.csv", content)
        self.assertEqual(job["stage"], STAGE_MAPPING_REVIEW)
        payload = self._mapping_review(job)
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=payload["proposed_mapping"], entity=payload["entity"], approved_by="staff-1")
        self.assertEqual((job["status"], job["stage"]), (JOB_FAILED, STAGE_NORMALIZING))
        self.assertIn("none of the 2 rows can be imported", job["error"])
        self.assertIn("marks_exceed_maximum", job["error"])
        self.assertEqual(self.store.count_records("college_a", "exam"), 0)
        # A retry maps the file again: the mapping that imported nothing was not kept.
        retried = await self.service.process("college_a", job["job_id"])
        self.assertEqual((retried["status"], retried["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        self.assertFalse(retried["mapping"]["profile_applied"])

    async def test_retrying_a_job_that_imported_nothing_under_a_remembered_mapping_asks_again(self):
        from app.normalization.mapping import header_signature

        # A mapping remembered before it was known to import nothing (as approvals used to be).
        headers = ["USN", "Student Name", "Subject Code", "Exam", "Total Marks", "Max Marks"]
        self.store.save_mapping_profile("college_a", entity="exam", header_signature=header_signature(headers), mapping={"USN": "student_id", "Student Name": "student_name", "Subject Code": "course_code", "Exam": "exam_name", "Total Marks": "marks_obtained", "Max Marks": "max_marks"}, approved_by="staff-1")
        job = await self._upload("ia.csv", b"USN,Student Name,Subject Code,Exam,Total Marks,Max Marks\n1MS23MBA001,Ravi,MBA101,IA1,42,25\n")
        self.assertEqual((job["status"], job["stage"]), (JOB_FAILED, STAGE_NORMALIZING))
        self.assertTrue(job["mapping"]["profile_applied"])
        # Retry puts the mapping in front of a person, with the reason, instead of failing the same way.
        retried = await self.service.process("college_a", job["job_id"])
        self.assertEqual((retried["status"], retried["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        payload = self._mapping_review(retried)
        self.assertIn("none of the 1 rows can be imported", payload["previous_error"])
        # Swapping the two marks columns imports the row and replaces the remembered mapping.
        mapping = dict(payload["proposed_mapping"], **{"Total Marks": "max_marks", "Max Marks": "marks_obtained"})
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=mapping, entity="exam", approved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(self.store.find_mapping_profile("college_a", "exam", header_signature(headers))["mapping"]["Total Marks"], "max_marks")

    async def test_a_failed_approval_can_be_retried_back_to_review(self):
        from unittest import mock

        from app.ingestion.service import ProcessingError

        content = b"Name,ID,Contact,Prog,Semester,Remarks\nRavi Kumar,1MS23MBA001,9876543210,MBA,2,fine\n"
        job = await self._upload("list.csv", content)
        payload = self._mapping_review(job)
        with mock.patch.object(type(self.store), "lookup_person_matches", side_effect=RuntimeError("connection reset")), self.assertLogs("app.ingestion.service", level="ERROR"):
            with self.assertRaises(ProcessingError):
                await self.service.apply_mapping("college_a", job["job_id"], mapping=payload["proposed_mapping"], entity="student", approved_by="staff-1")
        self.assertEqual(self.store.get_job("college_a", job["job_id"])["status"], JOB_FAILED)
        retried = await self.service.process("college_a", job["job_id"])
        self.assertEqual((retried["status"], retried["stage"]), (JOB_NEEDS_REVIEW, STAGE_MAPPING_REVIEW))
        payload = self._mapping_review(retried)
        job = await self.service.apply_mapping("college_a", job["job_id"], mapping=payload["proposed_mapping"], entity="student", approved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)

    async def test_conflicting_copies_import_the_copy_the_reviewer_chose(self):
        rows = b"Name,USN,Program,Semester,Phone\nNeha Shah,{usn},BCA,1,9811111111\nNeha Shah,{usn},BCA,2,9811111112\n"
        keep_earlier = await self._upload("a.csv", rows.replace(b"{usn}", b"1AB22CS010"), entity_hint="student")
        self.assertEqual(keep_earlier["stage"], STAGE_DUPLICATE_REVIEW)
        job = await self._decide_duplicates(keep_earlier, "approved")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(self.store.get_record("college_a", "student", "1ab22cs010")["semester"], 1)
        self.assertEqual(job["report"]["import"]["skipped_reasons"], {"duplicate_review_decision": 1})
        use_this = await self._upload("b.csv", rows.replace(b"{usn}", b"1AB22CS011"), entity_hint="student")
        job = await self._decide_duplicates(use_this, "rejected")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        self.assertEqual(self.store.get_record("college_a", "student", "1ab22cs011")["semester"], 2)
        # A later copy that cannot be imported raises no question: the good copy is imported.
        broken = await self._upload("c.csv", b"Name,USN,Program,Semester,Phone\nIra Menon,1AB22CS012,BCA,1,9822222221\n,1AB22CS012,BCA,2,9822222222\n", entity_hint="student")
        self.assertEqual(broken["status"], JOB_IMPORTED)
        self.assertEqual(self.store.get_record("college_a", "student", "1ab22cs012")["semester"], 1)

    async def test_only_decisions_on_the_current_run_count(self):
        from datetime import datetime, timedelta, timezone

        rows = b"Name,USN,Program,Semester,Phone\nIra Menon,1AB22CS020,BCA,1,9833333331\nIra Menon,1AB22CS020,BCA,2,9833333332\n"
        job = await self._upload("a.csv", rows, entity_hint="student", options={"auto_commit": False})
        self.assertEqual(job["stage"], STAGE_DUPLICATE_REVIEW)
        job = await self._decide_duplicates(job, "approved")  # keep the earlier copy
        self.assertEqual(job["status"], JOB_READY)
        # The job is interrupted and restarted before the commit: the question is asked again and answered differently.
        stale = (datetime.now(timezone.utc) - timedelta(seconds=self.service.restart_after_seconds + 60)).isoformat()
        self.store.update_job("college_a", job["job_id"], status=JOB_PROCESSING, stage=STAGE_NORMALIZING)
        self.store.backend.execute("UPDATE ingestion_jobs SET updated_at = ? WHERE job_id = ?", (stale, job["job_id"]))
        job = await self.service.process("college_a", job["job_id"])
        self.assertEqual(job["stage"], STAGE_DUPLICATE_REVIEW)
        for item in self.store.list_review_items("college_a", job_id=job["job_id"]):
            job = self.service.resolve_review("college_a", item["review_id"], decision="rejected", resolved_by="staff-1")  # use this row
        job = self.service.commit("college_a", job["job_id"], committed_by="staff-1")
        self.assertEqual(job["report"]["import"]["inserted"], 1)
        self.assertEqual(self.store.get_record("college_a", "student", "1ab22cs020")["semester"], 2)

    async def test_a_copy_that_cannot_be_imported_does_not_claim_the_identifier(self):
        # The first copy has no name (a blocking error); the second is fine.
        content = b"Name,USN,Program,Semester,Phone\n,1AB22CS030,BCA,1,9844444441\nIra Menon,1AB22CS030,BCA,2,9844444442\n"
        job = await self._upload("a.csv", content, entity_hint="student")
        self.assertEqual(job["status"], JOB_IMPORTED, "no question is asked about a row that cannot be imported")
        self.assertEqual(self.store.get_record("college_a", "student", "1ab22cs030")["semester"], 2)

    async def test_duplicate_questions_come_in_file_order(self):
        lines = [b"Name,USN,Program,Semester,Phone"]
        for i in range(12):
            lines.append(f"Person {i} Rao,1AB22CS{i:03d},BCA,1,98555{i:05d}".encode())
            lines.append(f"Person {i} Rao,1AB22CS{i:03d},BCA,2,98666{i:05d}".encode())
        job = await self._upload("pairs.csv", b"\n".join(lines) + b"\n", entity_hint="student")
        items = self.store.list_review_items("college_a", job_id=job["job_id"])
        rows = [int(item["payload"]["left_locator"].split("row=")[1]) for item in items]
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows, sorted(rows))

    async def test_same_person_decision_drops_every_copy_of_the_new_identity(self):
        await self._upload("students.csv", STUDENTS)
        # A new USN for an existing person, listed twice with different details.
        content = (
            b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\n"
            b"Ravi Kumar,1MS23MBA777,MBA,1,9876543210,ravi@x.com,12/05/2003\n"
            b"Ravi Kumar,1MS23MBA777,MBA,2,9876543210,ravi@x.com,12/05/2003\n"
        )
        job = await self._upload("again.csv", content)
        for item in self.store.list_review_items("college_a", job_id=job["job_id"]):
            decision = "approved" if item["payload"]["kind"] == "probable_person" else "rejected"
            job = self.service.resolve_review("college_a", item["review_id"], decision=decision, resolved_by="staff-1")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertIsNone(self.store.get_record("college_a", "student", "1ms23mba777"))

    async def test_every_duplicate_decision_counts_however_many_there_are(self):
        from unittest import mock

        await self._upload("students.csv", STUDENTS)
        lines = [b"Student Name,USN,Course,Sem,Phone,Email ID,DOB"] + [f"Person{i} Surname,1MS23MBA{i:03d},MBA,1,98765{i:05d},p{i}@x.com,2003-01-{i - 90}".encode() for i in range(100, 105)]
        await self._upload("first.csv", b"\n".join(lines) + b"\n")
        renumbered = b"\n".join(lines).replace(b"1MS23MBA", b"1MS23MCA") + b"\n"
        job = await self._upload("second.csv", renumbered)
        self.assertEqual(len(self.store.list_review_items("college_a", job_id=job["job_id"])), 5)
        # Pages of two: the decisions on later pages must still be honoured at commit.
        with mock.patch("app.ingestion.service.REVIEW_PAGE_SIZE", 2):
            job = await self._decide_duplicates(job, "approved")
        self.assertEqual(job["status"], JOB_IMPORTED)
        self.assertEqual(job["report"]["import"]["inserted"], 0)
        self.assertEqual(job["report"]["import"]["skipped_reasons"], {"duplicate_review_decision": 5})

    async def test_a_failure_before_the_import_is_recorded_on_the_job(self):
        from unittest import mock

        await self._upload("students.csv", STUDENTS)
        same_person = b"Student Name,USN,Course,Sem,Phone,Email ID,DOB\nRavi Kumar,1MS23MBA777,MBA,1,9876543210,ravi@x.com,12/05/2003\n"
        job = await self._upload("again.csv", same_person)
        review = self.store.list_review_items("college_a", job_id=job["job_id"])[0]
        # Working out which rows the decision skips reads the store before anything is imported.
        with mock.patch.object(type(self.store), "existing_keys", side_effect=RuntimeError("connection reset")):
            with self.assertLogs("app.ingestion.service", level="ERROR"), self.assertRaises(CommitError):
                self.service.resolve_review("college_a", review["review_id"], decision="approved", resolved_by="staff-1")
        job = self.store.get_job("college_a", job["job_id"])
        self.assertEqual((job["status"], job["stage"]), (JOB_READY, STAGE_READY))
        self.assertIn("RuntimeError", job["error"])
        self.assertEqual(self.service.commit("college_a", job["job_id"], committed_by="staff-1")["status"], JOB_IMPORTED)


if __name__ == "__main__":
    unittest.main()
