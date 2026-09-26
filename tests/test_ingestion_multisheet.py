"""Every usable sheet of an uploaded workbook is imported, not only the largest."""

import unittest
from unittest import mock

from app.api.routes.ingestion import _public_job
from app.ingestion.registry import ParserRegistry
from app.ingestion.service import JOB_FAILED, JOB_IMPORTED, JOB_QUEUED, IngestionService
from app.institution_data.store import InstitutionDataStore
from app.storage.object_store import InMemoryObjectStore
from app.workers.handlers import register_handlers
from app.workers.queue import InlineJobQueue, JobQueue
from test_ingestion_parsers import build_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def roster(title, header, rows):
    """A sheet laid out like a college roster: a title row, blank rows, the table from row 5 at column C."""

    return [[title], [], [], [], ["", "", *header], *[["", "", *row] for row in rows]]


GM = roster("MBA 2025-27 GM", ["Sl.no", "Name", "USN No", "Mail ID", "Student phone no"], [
    [1, "Asha Rao", "1AB25MBA001", "asha@example.com", "9000000001"],
    [2, "Ravi Kumar", "1AB25MBA002", "ravi@example.com", "9000000002"],
    [3, "Meena Das", "1AB25MBA003", "meena@example.com", "9000000003"],
])
# The same columns, spelled a little differently, with a second address-like repeat left out.
DM = roster("MBA 2025-27 DM", ["Sl no", "NAME", "USN  No", "Mail ID", "Student Phone No"], [
    [1, "Kiran Shah", "1AB25MBA101", "kiran@example.com", "9000000101"],
    [2, "Leela Iyer", "1AB25MBA102", "leela@example.com", "9000000102"],
])


class MultiSheetUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = InstitutionDataStore(":memory:")
        self.objects = InMemoryObjectStore()
        self.service = IngestionService(store=self.store, objects=self.objects, parsers=ParserRegistry())

    def _upload(self, sheets, **kwargs):
        return self.service.upload("college_a", "staff-1", file_name="students.xlsx", content=build_xlsx(sheets), content_type=XLSX, **kwargs)

    async def test_sheets_with_the_same_columns_are_one_job_with_a_sheet_column(self):
        job = await self.service.process("college_a", self._upload({"GM": GM, "DM": DM}, entity_hint="student")["job_id"])
        self.assertEqual(job["status"], JOB_IMPORTED, job.get("error"))
        self.assertEqual(job["row_count"], 5)
        self.assertEqual(job["report"]["import"]["inserted"], 5)
        self.assertEqual(job["report"]["selected_table"]["headers"][-1], "Sheet")
        self.assertEqual(job["report"]["sheets"], {"this_job": ["GM", "DM"], "other_jobs": [], "skipped": []})
        rows = self.store.job_records("college_a", job["job_id"])
        self.assertEqual([row["locator"] for row in rows], [f"sheet=GM;row={n}" for n in (6, 7, 8)] + [f"sheet=DM;row={n}" for n in (6, 7)])
        self.assertEqual([row["raw"]["Sheet"] for row in rows], ["GM"] * 3 + ["DM"] * 2)
        # A DM row is staged under the first sheet's spelling of each header.
        self.assertEqual(rows[3]["raw"]["USN No"], "1AB25MBA101")
        self.assertNotIn("USN  No", rows[3]["raw"])
        record = self.store.get_record("college_a", "student", "1ab25mba101")
        self.assertEqual(record["lineage"]["source_locator"], "sheet=DM;row=6")

    async def test_a_sheet_with_other_columns_becomes_a_sibling_job_the_queue_processes(self):
        queue = InlineJobQueue(self.store)
        register_handlers(queue, ingestion=self.service)
        contacts = [["Student Data 2023-25"], ["USN No", "Name", "Seat Type", "Contact Number"], ["1AB23MBA001", "Asha Rao", "GM", "9000000001"], ["1AB23MBA002", "Ravi Kumar", "SNQ", "9000000002"]]
        emails = [["Student Data 2023-25"], ["USN No", "Name", "Seat Type", "Email_ID"], ["1AB23MBA001", "Asha Rao", "GM", "asha@example.com"], ["1AB23MBA002", "Ravi Kumar", "SNQ", "ravi@example.com"]]
        upload = self._upload({"Sheet1": contacts, "Sheet2": emails, "Sheet4": []}, entity_hint="student")
        queue.enqueue("college_a", "ingestion.process", {"institution_id": "college_a", "job_id": upload["job_id"], "requested_by": "staff-1"})
        job = self.store.get_job("college_a", upload["job_id"])
        self.assertEqual(job["status"], JOB_IMPORTED, job.get("error"))
        sheets = job["report"]["sheets"]
        self.assertEqual(sheets["this_job"], ["Sheet1"])
        self.assertEqual(sheets["skipped"], [{"sheet": "Sheet4", "reason": "no header row was found (the sheet is empty or holds no table)"}])
        [other] = sheets["other_jobs"]
        self.assertEqual(other["sheets"], ["Sheet2"])
        self.assertEqual(_public_job(job)["sibling_job_ids"], [other["job_id"]])
        sibling = self.store.get_job("college_a", other["job_id"])
        self.assertEqual(sibling["status"], JOB_IMPORTED, sibling.get("error"))
        self.assertEqual((sibling["options"]["sheets"], sibling["options"]["origin_job_id"], sibling["row_count"]), (["Sheet2"], job["job_id"], 2))
        self.assertNotIn("Sheet", sibling["report"]["selected_table"]["headers"])
        # One stored file serves both jobs.
        self.assertEqual(sibling["file_id"], job["file_id"])
        self.assertEqual(len(self.objects._objects), 1)
        self.assertEqual(self.store.get_record("college_a", "student", "1ab23mba001")["email"], "asha@example.com")
        # Parsing the file again finds the sibling it made rather than making another.
        self.store.update_job("college_a", job["job_id"], status=JOB_FAILED, stage="parsing")
        again = await self.service.process("college_a", job["job_id"])
        self.assertEqual([item["job_id"] for item in again["report"]["sheets"]["other_jobs"]], [other["job_id"]])
        self.assertEqual(len(self.store.list_jobs("college_a")), 2)

    async def test_a_deferred_queue_runs_the_sibling_later_and_a_refusal_is_recorded(self):
        queue = JobQueue(self.store)  # records jobs until a worker picks them up
        register_handlers(queue, ingestion=self.service)
        sheets = {"Marks": [["USN", "Name", "Course Code", "Exam", "Marks"], ["1AB23MBA001", "Asha Rao", "MBA201", "SEE", 71]], "Roster": [["USN", "Name", "Phone"], ["1AB23MBA001", "Asha Rao", "9000000001"]]}
        upload = self._upload(sheets)
        queue.enqueue("college_a", "ingestion.process", {"institution_id": "college_a", "job_id": upload["job_id"], "requested_by": "staff-1"})
        self.assertEqual(queue.run_pending_blocking(), 1)
        [sibling_id] = _public_job(self.store.get_job("college_a", upload["job_id"]))["sibling_job_ids"]
        self.assertEqual(self.store.get_job("college_a", sibling_id)["status"], JOB_QUEUED)
        self.assertEqual(queue.run_pending_blocking(), 1)
        self.assertNotEqual(self.store.get_job("college_a", sibling_id)["status"], JOB_QUEUED)
        # A queue that refuses the sibling leaves it failed with the reason, ready for a retry.
        upload = self._upload(sheets)
        queue.enqueue("college_a", "ingestion.process", {"institution_id": "college_a", "job_id": upload["job_id"], "requested_by": "staff-1"})
        with mock.patch.object(queue, "enqueue", side_effect=RuntimeError("queue unavailable")):
            queue.run_pending_blocking()
        [sibling_id] = _public_job(self.store.get_job("college_a", upload["job_id"]))["sibling_job_ids"]
        refused = self.store.get_job("college_a", sibling_id)
        self.assertEqual(refused["status"], JOB_FAILED)
        self.assertIn("queue unavailable", refused["error"])

    async def test_empty_sheets_blank_forms_and_headerless_sheets_are_skipped_with_the_reason(self):
        staff = [["Staff details"], ["Sl.", "Name", "Designation", "Emp.code"], [1, "Asha Rao", "Professor", "EMP001"], [2, "Ravi Kumar", "Assistant Professor", "EMP002"]]
        leavers = [["Test Person", "Assistant Professor", "01.08.2024", "9000000009", "test.person@example.com"], ["Other Person", "Professor", "02.09.2023", "9000000010", "other.person@example.com"]]
        form = [["Sl.No", "Particulars", "Details"], [1, "Name", ""], [2, "Designation", ""], [3, "Date of joining", ""]]
        job = await self.service.process("college_a", self._upload({"Sheet1": staff, "Exit": leavers, "Sheet3": form, "Sheet4": []})["job_id"])
        sheets = job["report"]["sheets"]
        self.assertEqual((sheets["this_job"], sheets["other_jobs"]), (["Sheet1"], []))
        reasons = {item["sheet"]: item["reason"] for item in sheets["skipped"]}
        self.assertEqual(set(reasons), {"Exit", "Sheet3", "Sheet4"})
        self.assertIn("no header row", reasons["Exit"])
        self.assertIn("fewer than two columns hold values", reasons["Sheet3"])
        self.assertEqual(job["row_count"], 2)
        # A workbook with nothing to import says which sheet was skipped and why.
        failed = await self.service.process("college_a", self._upload({"Sheet3": form, "Sheet4": []})["job_id"])
        self.assertEqual(failed["status"], JOB_FAILED)
        self.assertIn("Sheet3: fewer than two columns", failed["error"])

    async def test_a_two_column_sheet_whose_data_counts_up_is_still_imported(self):
        # Only a row-number column ("Sl no") counting 1, 2, 3 is discounted; a
        # data column that happens to count (a semester) keeps the sheet usable.
        semesters = roster("Semester list", ["USN No", "Semester"], [["1AB25MBA001", 1], ["1AB25MBA002", 2], ["1AB25MBA003", 3]])
        job = await self.service.process("college_a", self._upload({"Sem": semesters}, entity_hint="student")["job_id"])
        self.assertNotIn("fewer than two columns", job.get("error") or "")
        self.assertEqual(job["row_count"], 3)

    async def test_an_explicit_sheet_imports_only_that_sheet(self):
        job = await self.service.process("college_a", self._upload({"GM": GM, "DM": DM}, entity_hint="student", options={"sheet": "DM"})["job_id"])
        self.assertEqual(job["status"], JOB_IMPORTED, job.get("error"))
        self.assertEqual((job["row_count"], job["sheet_name"]), (2, "DM"))
        self.assertNotIn("sheets", job["report"])
        self.assertNotIn("Sheet", job["report"]["selected_table"]["headers"])
        self.assertEqual(len(self.store.list_jobs("college_a")), 1)


if __name__ == "__main__":
    unittest.main()
