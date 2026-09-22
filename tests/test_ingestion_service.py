import unittest

from app.ingestion.registry import ParserRegistry
from app.ingestion.service import JOB_IMPORTED, JOB_NEEDS_REVIEW, STAGE_DUPLICATE_REVIEW, STAGE_MAPPING_REVIEW, IngestionError, IngestionService
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


if __name__ == "__main__":
    unittest.main()
