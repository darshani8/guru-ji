import unittest

from app.ingestion.models import FileKind, ParseResult
from app.ingestion.parsers.tabular import grid_to_table
from app.ingestion.registry import ParserRegistry
from app.ingestion.service import JOB_IMPORTED, IngestionService
from app.ingestion.wide_marks import EXAM_HEADERS, reshape_marks_sheets, reshape_marks_table
from app.institution_data.store import InstitutionDataStore
from app.storage.object_store import InMemoryObjectStore
from test_ingestion_parsers import build_xlsx

# Made-up students and subjects in the layout of a semester result sheet: a
# title, then one column per subject under EXTERNAL and INTERNAL group labels
# (as the two header rows read once combined), then Total and Percentage.
GROUPED = [
    ["MBA SECOND SEMESTER RESULT ANALYSIS"],
    ["Sl. No", "USN No", "Name", "EXTERNAL MARKS Accounts (MB101)", "EXTERNAL MARKS Economics (MB102)", "EXTERNAL MARKS Statistics (MB103)",
     "INTERNAL MARKS Accounts (MB101)", "INTERNAL MARKS Economics (MB102)", "INTERNAL MARKS Statistics (MB103)", "Total", "Percentage (2nd Sem)"],
    [1, "1XX25MB001", "Test Student One", 61, 55, 70, 42, 38.5, 45, 311.5, 62.3],
    [2, "1XX25MB002", "Test Student Two", "AB", 48, 52, 40, 35, "", 215, 43],
    [3, "1XX25MB003", "Test Student Three", 30, "F", 66, 44, 41, 47, 228, 45.6],
    ["", "", "Prepared by the exam cell"],
]
PLAIN = [
    ["USN", "Student Name", "Accounts (MB101)", "Economics [MB102]", "MB103", "Business Presentation (MB104)", "Result"],
    ["1XX25MB001", "Test Student One", 61, 55, 70, 20, "PASS"],
    ["1XX25MB002", "Test Student Two", 50, 48, 52, "\u2013", "PASS"],
]


def _table(grid, name="2nd Sem"):
    return grid_to_table(grid, name=name, source_file="marks.xlsx", sheet=name)


class WideMarksReshapeTests(unittest.TestCase):
    def test_grouped_subject_columns_become_one_exam_row_per_student_course_and_exam(self):
        reshaped, summary = reshape_marks_table(_table(GROUPED))
        self.assertEqual(reshaped.headers, EXAM_HEADERS)
        # 3 students x 6 subject columns, less the one blank cell.
        self.assertEqual((summary["wide_rows"], summary["exam_rows"], summary["student_rows"], reshaped.row_count), (4, 17, 3, 17))
        self.assertIn("reshaped_4_wide_rows_to_17_exam_rows", reshaped.warnings)
        rows = {(record.fields["student_id"], record.fields["course_code"], record.fields["exam_name"]): record for record in reshaped.records}
        self.assertEqual(len(rows), 17)
        first = rows[("1XX25MB001", "MB101", "SEE (External)")]
        self.assertEqual(first.fields, {
            "student_id": "1XX25MB001", "student_name": "Test Student One", "course_code": "MB101", "course_name": "Accounts",
            "exam_name": "SEE (External)", "marks_obtained": 61, "result_status": None,
        })
        self.assertEqual((first.locator, first.row_number, first.sheet), ("sheet=2nd Sem;row=3;col=D", 3, "2nd Sem"))
        self.assertEqual(rows[("1XX25MB001", "MB102", "CIE (Internal)")].fields["marks_obtained"], 38.5)
        self.assertEqual(rows[("1XX25MB001", "MB102", "CIE (Internal)")].locator, "sheet=2nd Sem;row=3;col=H")
        absent = rows[("1XX25MB002", "MB101", "SEE (External)")].fields
        self.assertEqual((absent["marks_obtained"], absent["result_status"]), (None, "AB"))
        self.assertEqual(rows[("1XX25MB003", "MB102", "SEE (External)")].fields["result_status"], "F")
        self.assertNotIn(("1XX25MB002", "MB103", "CIE (Internal)"), rows)
        self.assertEqual({record.fields["exam_name"] for record in reshaped.records}, {"SEE (External)", "CIE (Internal)"})
        # Columns outside the exam rows, and the rows skipped, are named in the summary.
        self.assertEqual(summary["left_out_columns"], ["Sl. No", "Total", "Percentage (2nd Sem)"])
        self.assertEqual((summary["skipped_rows"], summary["skipped_row_details"]), (1, [{"row": 6, "reason": "no student identifier and no marks"}]))
        self.assertEqual(summary["blank_mark_cells"], 1)
        self.assertEqual(summary["course_columns"]["INTERNAL MARKS Statistics (MB103)"], {"course_code": "MB103", "course_name": "Statistics", "exam_name": "CIE (Internal)"})

    def test_plain_subject_code_headers_are_reshaped_as_marks(self):
        reshaped, summary = reshape_marks_table(_table(PLAIN, name="Sheet1"))
        # A dash is no mark: counted as a blank cell, not staged.
        self.assertEqual((summary["exam_rows"], summary["blank_mark_cells"]), (7, 1))
        first = reshaped.records[0].fields
        self.assertEqual((first["course_code"], first["course_name"], first["exam_name"], first["student_name"]), ("MB101", "Accounts", "Marks", "Test Student One"))
        self.assertEqual({record.fields["course_code"] for record in reshaped.records}, {"MB101", "MB102", "MB103", "MB104"})
        self.assertIsNone(next(record for record in reshaped.records if record.fields["course_code"] == "MB103").fields["course_name"])
        self.assertEqual(summary["left_out_columns"], ["Result"])

    def test_rows_without_an_identifier_are_reported_never_dropped_silently(self):
        grid = [
            ["USN", "Name", "SEE Accounts (MB101)", "SEE Economics (MB102)", "SEE Statistics (MB103)", "", "Total"],
            ["", "Unknown", 40, 41, 42, "", 123],
            ["1XX25MB001", "One", 50, 51, 52, "", 153],
            ["1XX25MB002", "Two", "", "", "", "", ""],
            ["", "", "LJ", "HK", "SS", "", ""],
        ]
        reshaped, summary = reshape_marks_table(_table(grid))
        # Marks with no USN are staged (validation rejects them where the uploader sees it).
        self.assertEqual([record.fields["student_id"] for record in reshaped.records], [None] * 3 + ["1XX25MB001"] * 3)
        self.assertEqual((summary["rows_without_marks"], summary["skipped_row_details"]), ([4], [{"row": 5, "reason": "no student identifier and no marks"}]))
        # A blank unnamed column is not listed; a named one is, even when it is empty.
        self.assertEqual(summary["left_out_columns"], ["Total"])

    def test_a_repeated_subject_column_stays_a_separate_exam(self):
        grid = [["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Accounts (MB101)", "Total"], ["1XX25MB001", "One", 10, 20, 30, 40, 100]]
        reshaped, _ = reshape_marks_table(_table(grid))
        accounts = sorted((record.fields["exam_name"], record.fields["marks_obtained"]) for record in reshaped.records if record.fields["course_code"] == "MB101")
        self.assertEqual(accounts, [("Marks", 10), ("Marks (2)", 40)])

    def test_tables_that_are_not_marks_sheets_are_left_alone(self):
        students = _table([["USN", "Name", "Program (MBA)", "Semester", "Phone", "Batch (2025)"], ["1XX25MB001", "One", "MBA", 2, "9000000001", 2025]], name="GM")
        attendance = _table([["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)"], ["1XX25MB001", "One", 85, 90, 72.5]], name="Attendance Aug")
        no_identifier = _table([["Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total"], ["One", 85, 90, 72, 247]])
        too_few = _table([["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Total"], ["1XX25MB001", "One", 85, 90, 175]])
        faculty = _table([["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total"], ["1XX25MB001", "One", "AB", "CD", "EF", "GH"]])
        for table in (students, attendance, no_identifier, too_few, faculty):
            self.assertIsNone(reshape_marks_table(table), table.name)
        result = ParseResult(file_name="students.xlsx", file_kind=FileKind.XLSX, tables=[students, attendance])
        self.assertIs(reshape_marks_sheets(result), result)
        # A marks sheet the uploader named another entity for is not reshaped.
        marks = ParseResult(file_name="marks.xlsx", file_kind=FileKind.XLSX, tables=[_table(GROUPED)])
        self.assertIs(reshape_marks_sheets(marks, entity="student"), marks)
        self.assertEqual(reshape_marks_sheets(marks).tables[0].headers, EXAM_HEADERS)


class WideMarksImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wide_marks_sheet_imports_as_exam_records(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        content = build_xlsx({"2nd Sem": GROUPED})
        job = service.upload("college_a", "staff-1", file_name="result.xlsx", content=content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        job = await service.process("college_a", job["job_id"])
        self.assertEqual((job["entity"], job["status"]), ("exam", JOB_IMPORTED), job.get("error") or job.get("mapping"))
        self.assertEqual(job["report"]["import"]["inserted"], 17)
        self.assertEqual(job["report"]["selected_table"]["headers"], list(EXAM_HEADERS))
        self.assertIn("reshaped_4_wide_rows_to_17_exam_rows", job["report"]["selected_table"]["warnings"])
        summary = job["report"]["parse"]["metadata"]["reshaped_marks"][0]
        self.assertEqual((summary["sheet"], summary["wide_rows"], summary["exam_rows"]), ("2nd Sem", 4, 17))
        self.assertEqual(summary["left_out_columns"], ["Sl. No", "Total", "Percentage (2nd Sem)"])
        exams = store.query_records("college_a", "exam", limit=100)
        self.assertEqual(len(exams), 17)
        by_key = {(row["student_id"], row["course_code"], row["exam_name"]): row for row in exams}
        self.assertEqual(by_key[("1XX25MB001", "MB103", "SEE (External)")]["marks_obtained"], 70)
        self.assertEqual(by_key[("1XX25MB001", "MB103", "SEE (External)")]["course_name"], "Statistics")
        self.assertEqual(by_key[("1XX25MB002", "MB101", "SEE (External)")]["result_status"], "ab")  # statuses are stored lower-case
        self.assertIsNone(by_key[("1XX25MB002", "MB101", "SEE (External)")].get("marks_obtained"))
        self.assertEqual(by_key[("1XX25MB003", "MB101", "CIE (Internal)")]["lineage"]["source_locator"], "sheet=2nd Sem;row=5;col=G")


if __name__ == "__main__":
    unittest.main()
