import unittest
from collections import Counter

from app.ingestion.models import FileKind, ParseResult
from app.ingestion.parsers.tabular import grid_to_table
from app.ingestion.registry import ParserRegistry
from app.ingestion.service import JOB_IMPORTED, JOB_NEEDS_REVIEW, IngestionService
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

# The same layout as the workbook reads it: a title over the table, the group labels
# merged over their subjects in one header row and the subjects in the next, a row of
# faculty initials under the subjects, Total and Percentage merged down the three rows,
# and a legend beside the table.
SUBJECTS = ["Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Marketing (MB104)", "Finance (MB105)", "Law (MB106)"]
LEGEND = "Legend: AB means absent, F means failed"
RESULT_WORKBOOK = [
    [None, "MBA SECOND SEMESTER RESULT"],
    ["Sl. No", "USN No", "Name", "EXTERNAL MARKS", None, None, None, None, None, "INTERNAL MARKS", None, None, None, None, None, "Total", "Percentage (2nd Sem)", None, LEGEND],
    [None, None, None, *SUBJECTS, *SUBJECTS, None, None, None, None, "Pass - external filled"],
    [None, None, None, *["PQ", "RS", "TU", "VW", "XY", "ZA"] * 2, None, None, None, None, "Initials of the faculty for each subject"],
    *[[index, f"1XX25MB00{index}", f"Test Student {index}", *[40 + index + column for column in range(12)], 546 + 12 * index, 45.5 + index] for index in range(1, 5)],
]
RESULT_MERGES = ["B1:P1", "A2:A4", "B2:B4", "C2:C4", "D2:I2", "J2:O2", "P2:P4", "Q2:Q4"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _table(grid, name="2nd Sem"):
    return grid_to_table(grid, name=name, source_file="marks.xlsx", sheet=name)


def _internal_assessment(mark, title="INTERNAL ASSESSMENT MARKS"):
    return [[title], ["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total"]] + [
        [f"1XX25MB00{index}", f"Test Student {index}", mark, mark, mark, 3 * mark] for index in range(1, 5)]


def _csv(marks):
    """A marks sheet as CSV that names no exam anywhere: its exam is "Marks"."""

    rows = [["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total Marks"]] + [
        [f"1XX25MB00{index}", f"Test Student {index}", *(marks.get((index, code), 10) for code in ("MB101", "MB102", "MB103")), 30] for index in range(1, 5)]
    return ("\n".join(",".join(str(cell) for cell in row) for row in rows) + "\n").encode()


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
        reshaped, _ = reshape_marks_table(_table(grid, name="Marks"))
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

    def test_attendance_sheets_are_never_read_as_marks(self):
        # Classes attended per subject, with a Total and a Percentage column: a number per
        # student and subject, just like marks. A word of attendance anywhere rules it out.
        monthly = _table([["ATTENDANCE FOR THE MONTH OF AUGUST"], ["Sl No", "USN", "Name", "HRM (MBA201)", "FM (MBA202)", "MM (MBA203)", "Total", "Percentage"]]
                         + [[index, f"1XX25MB00{index}", f"Student {index}", 20 - index, 18, 22, 60 - index, 85.7] for index in range(1, 5)], name="Aug")
        semester = _table([["USN", "Name", "HRM (MBA201)", "FM (MBA202)", "MM (MBA203)", "Total Classes Attended"]]
                          + [[f"1XX25MB00{index}", f"Student {index}", 20, 18, 22, 60] for index in range(1, 5)], name="2nd Sem")
        titled = _table([["CLASSES HELD AND ATTENDED - RESULT OF THE MONTH"], ["USN", "Name", "HRM (MBA201)", "FM (MBA202)", "MM (MBA203)"]]
                        + [[f"1XX25MB00{index}", f"Student {index}", 20, 18, 22] for index in range(1, 5)], name="Sheet1")
        # Attendance abbreviated in the headers (Att., Attd, Atten.), beside marks or on its own.
        beside = _table([["USN", "Name", "IA Accounts (MB101)", "IA Economics (MB102)", "IA Statistics (MB103)", "Att. Accounts (MB101)", "Att. Economics (MB102)", "Att. Statistics (MB103)"]]
                        + [[f"1XX25MB00{index}", f"Student {index}", 18, 19, 20, 85.5, 90.0, 76.4] for index in range(1, 5)], name="CIE")
        abbreviated = [_table([["USN", "Name", *[f"{word} {subject}" for subject in ("Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)")], "Result"]]
                              + [[f"1XX25MB00{index}", f"Student {index}", 30, 28, 33, "Eligible"] for index in range(1, 5)], name="Sem 2") for word in ("Att", "Attd", "Atten.")]
        for table in (monthly, semester, titled, beside, *abbreviated):
            self.assertIsNone(reshape_marks_table(table), table.headers)
        # A Total or a semester alone does not make a marks sheet either: nothing says the numbers are marks.
        self.assertIsNone(reshape_marks_table(_table([["USN", "Name", "HRM (MBA201)", "FM (MBA202)", "MM (MBA203)", "Total"], ["1XX25MB001", "One", 20, 18, 22, 60]])))

    def test_fee_sheets_and_amounts_are_never_read_as_marks(self):
        # An academic year in brackets is not a course code, and one "course" over every column is not a result sheet.
        fees = _table([["USN", "Name", "Tuition Fee (AY2025)", "Hostel Fee (AY2025)", "Transport Fee (AY2025)", "Total"], ["1XX25MB001", "One", 100, 60, 15, 175]], name="Fees")
        years = _table([["USN", "Name", "Fee (FY2024)", "Fee (FY2025)", "Fee (SEM2025)", "Result"], ["1XX25MB001", "One", 100, 60, 15, "PAID"]], name="Fees")
        one_code = _table([["USN", "Name", "Tuition (FEE2025)", "Hostel (FEE2025)", "Bus (FEE2025)", "Result"], ["1XX25MB001", "One", 100, 60, 15, "PAID"]], name="Fees")
        amounts = _table([["USN", "Name", "SEE Accounts (MB101)", "SEE Economics (MB102)", "SEE Statistics (MB103)"], ["1XX25MB001", "One", 45000, 30000, 12000]], name="Result")
        for table in (fees, years, one_code, amounts):
            self.assertIsNone(reshape_marks_table(table), table.headers)

    def test_grade_point_and_credit_sheets_are_never_read_as_marks(self):
        subjects = ["Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)"]
        named = _table([["USN", "Name", *subjects, "SGPA"], ["1XX25MB001", "One", 8, 9, 10, 9.0]], name="Grade Points")
        credits = _table([["Reg No", "Name", "MB101", "MB102", "MB103", "Total Credits", "Result"], ["1XX25MB001", "One", 4, 4, 3, 11, "PASS"]], name="Credits")
        titled = _table([["CREDITS EARNED"], ["USN", "Name", *subjects, "Result"], ["1XX25MB001", "One", 4, 4, 3, "PASS"]], name="2nd Sem")
        # An SGPA beside subjects that all hold 10 or less: grade points, whatever the sheet is called.
        beside = _table([["USN", "Name", *subjects, "SGPA"], ["1XX25MB001", "One", 8, 9, 10, 9.0], ["1XX25MB002", "Two", 7, 6, 8, 7.0]], name="Sheet1")
        for table in (named, credits, titled, beside):
            self.assertIsNone(reshape_marks_table(table), table.name)
        # Marks with an SGPA column, under a title naming the credit system, still are marks.
        marks = _table([["RESULT - CHOICE BASED CREDIT SYSTEM"], ["USN", "Name", *subjects, "SGPA"], ["1XX25MB001", "One", 61, 55, 70, 7.2]], name="2nd Sem")
        self.assertEqual(reshape_marks_table(marks)[1]["exam_rows"], 3)

    def test_a_subject_over_sub_columns_that_lost_their_headers_is_not_reshaped(self):
        # CIE, SEE and Total under each subject, their header row lost: which column is which exam is unknown.
        grid = [["USN", "Name", "HRM (MBA201)", "", "", "FM (MBA202)", "", "", "MM (MBA203)", "", ""],
                ["1XX25MB001", "One", 40, 50, 90, 41, 51, 92, 42, 52, 94], ["1XX25MB002", "Two", 30, 45, 75, 31, 46, 77, 32, 47, 79]]
        self.assertEqual(_table(grid).headers[3], "column_4")
        self.assertIsNone(reshape_marks_table(_table(grid, name="Result")))

    def test_exam_names_come_from_the_group_label_else_the_sheet_name(self):
        grid = [["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total Marks"], ["1XX25MB001", "One", 10, 20, 30, 60]]
        for sheet, exam in (("IA 1", "IA 1"), ("Mid Term Test", "Mid Term Test"), ("Sheet1", "Marks"), ("Table 2", "Marks"), ("data", "Marks"), ("2nd Sem", "2nd Sem")):
            reshaped, summary = reshape_marks_table(_table(grid, name=sheet))
            self.assertEqual({record.fields["exam_name"] for record in reshaped.records}, {exam}, sheet)
            self.assertEqual((summary["exam_names"], summary["courses"]), ([exam], ["MB101", "MB102", "MB103"]))

    def test_an_exam_the_sheet_name_does_not_name_comes_from_the_title_or_the_file_name(self):
        grid = [["USN", "Name", "Accounts (MB101)", "Economics (MB102)", "Statistics (MB103)", "Total Marks"], ["1XX25MB001", "One", 10, 20, 30, 60]]
        cases = (
            # (sheet, title, file name, exam): the first that names an exam wins, then a sheet name of its own, then "Marks".
            ("2nd Sem", "FIRST INTERNAL ASSESSMENT MARKS", "marks.xlsx", "FIRST INTERNAL ASSESSMENT"),
            ("2nd Sem", "XYZ COLLEGE OF MANAGEMENT MBA II SEM SECOND INTERNAL ASSESSMENT MARKS 2025", "marks.xlsx", "II SEM SECOND INTERNAL ASSESSMENT"),
            ("IA 2", "FIRST INTERNAL ASSESSMENT MARKS", "ia1.xlsx", "IA 2"),
            ("data", "", "IA2_marks final (1).csv", "IA2"),
            ("Sheet1", "MBA II SEM MARKS", "Test-1 marks.xlsx", "Test-1"),
            ("A Sec", "INTERNAL ASSESSMENT MARKS", "sections.xlsx", "CIE (Internal)"),
            ("A Sec", "MBA II SEM MARKS", "marks.xlsx", "A Sec"),
        )
        for sheet, title, file_name, exam in cases:
            table = grid_to_table(([[title]] if title else []) + grid, name=sheet, source_file=file_name, sheet=sheet)
            self.assertEqual(reshape_marks_table(table)[1]["exam_names"], [exam], (sheet, title, file_name))
        # A group label on the subjects still names the exam, whatever the title says.
        titled = grid_to_table([["FIRST INTERNAL ASSESSMENT MARKS"], *GROUPED[1:]], name="2nd Sem", source_file="ia1.xlsx", sheet="2nd Sem")
        self.assertEqual(reshape_marks_table(titled)[1]["exam_names"], ["SEE (External)", "CIE (Internal)"])

    def test_the_usn_is_the_student_id_even_after_an_admission_number(self):
        grid = [["Sl No", "Admission No", "USN", "Name", "SEE Accounts (MB101)", "SEE Economics (MB102)", "SEE Statistics (MB103)"],
                [1, "ADM0001", "1XX25MB001", "One", 50, 51, 52]]
        reshaped, summary = reshape_marks_table(_table(grid))
        self.assertEqual((summary["student_id_column"], summary["left_out_columns"]), ("USN", ["Sl No", "Admission No"]))
        self.assertEqual({record.fields["student_id"] for record in reshaped.records}, {"1XX25MB001"})
        # Without a USN (or register number, or student id), the admission number is the identifier.
        _, summary = reshape_marks_table(_table([row[:2] + row[3:] for row in grid]))
        self.assertEqual(summary["student_id_column"], "Admission No")
        # A class roll number before the USN is not the identifier either; without a USN it is, before an admission number.
        rolled = [["Roll No", *grid[0]], [7, *grid[1]]]
        _, summary = reshape_marks_table(_table(rolled))
        self.assertEqual((summary["student_id_column"], summary["left_out_columns"]), ("USN", ["Roll No", "Sl No", "Admission No"]))
        _, summary = reshape_marks_table(_table([row[:3] + row[4:] for row in rolled]))
        self.assertEqual(summary["student_id_column"], "Roll No")

    def test_a_grace_marked_cell_keeps_its_marks_and_its_text(self):
        # "45+2" is the 47 awarded (the grace marks added); "40*" the 40 awarded. The cell's text stays as the result status.
        grid = [["USN", "Name", "SEE Accounts (MB101)", "SEE Economics (MB102)", "SEE Statistics (MB103)"],
                ["1XX25MB001", "One", "45+2", "40*", 60], ["1XX25MB002", "Two", 55, "AB", 61], ["1XX25MB003", "Three", 56, 57, 62]]
        reshaped, summary = reshape_marks_table(_table(grid))
        cells = {(record.fields["student_id"], record.fields["course_code"]): (record.fields["marks_obtained"], record.fields["result_status"]) for record in reshaped.records}
        self.assertEqual(cells[("1XX25MB001", "MB101")], (47, "45+2"))
        self.assertEqual(cells[("1XX25MB001", "MB102")], (40, "40*"))
        self.assertEqual(cells[("1XX25MB001", "MB103")], (60, None))
        self.assertEqual(cells[("1XX25MB002", "MB102")], (None, "AB"))
        self.assertEqual(summary["grace_mark_cells"], 2)


class WideMarksImportTests(unittest.IsolatedAsyncioTestCase):
    async def _review(self, store, service, job, *, remember=True):
        """The job waits at the mapping review with the reshape summary; approving it as proposed imports it."""

        self.assertEqual((job["entity"], job["status"], job["stage"]), ("exam", JOB_NEEDS_REVIEW, "mapping_review"), job.get("error"))
        self.assertIsNone(job["report"].get("import"))
        [review] = [item for item in store.list_review_items("college_a", job_id=job["job_id"]) if item["kind"] == "mapping" and item["status"] == "pending"]
        self.assertEqual(review["payload"]["reshaped_marks"], job["report"]["reshaped_marks"])
        job = await service.apply_mapping("college_a", job["job_id"], mapping=review["payload"]["proposed_mapping"], entity="exam", approved_by="staff-1", remember=remember)
        self.assertEqual(job["status"], JOB_IMPORTED, job.get("error"))
        return review["payload"]["reshaped_marks"], job

    async def test_a_wide_marks_sheet_waits_for_review_then_imports_as_exam_records(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        content = build_xlsx({"2nd Sem": GROUPED})
        job = service.upload("college_a", "staff-1", file_name="result.xlsx", content=content, content_type=XLSX)
        job = await service.process("college_a", job["job_id"])
        # Nothing is imported before a person has seen what the reshape did.
        self.assertEqual(store.count_records("college_a", "exam"), 0)
        [shown], job = await self._review(store, service, job)
        self.assertEqual((shown["sheet"], shown["wide_rows"], shown["exam_rows"], shown["exam_names"], shown["courses"]), ("2nd Sem", 4, 17, ["SEE (External)", "CIE (Internal)"], ["MB101", "MB102", "MB103"]))
        self.assertEqual(shown["left_out_columns"], ["Sl. No", "Total", "Percentage (2nd Sem)"])
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
        # The mapping remembered on approval does not let the next reshaped upload skip the review.
        again = service.upload("college_a", "staff-1", file_name="result-again.xlsx", content=content, content_type=XLSX)
        again = await service.process("college_a", again["job_id"])
        _, again = await self._review(store, service, again)
        self.assertEqual((again["report"]["import"]["inserted"], again["report"]["import"]["unchanged"]), (0, 17))

    async def test_the_result_workbook_layout_gives_one_exam_row_per_student_course_and_exam(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        content = build_xlsx({"2nd Sem": RESULT_WORKBOOK}, merges={"2nd Sem": RESULT_MERGES})
        job = await service.process("college_a", service.upload("college_a", "staff-1", file_name="result.xlsx", content=content, content_type=XLSX)["job_id"])
        [shown], job = await self._review(store, service, job)
        self.assertEqual((shown["wide_rows"], shown["exam_rows"], shown["student_id_column"], shown["student_name_column"]), (4, 48, "USN No", "Name"))
        self.assertEqual((shown["exam_names"], shown["courses"]), (["SEE (External)", "CIE (Internal)"], ["MB101", "MB102", "MB103", "MB104", "MB105", "MB106"]))
        # The legend's "absent" is a note beside the table, not an attendance column.
        self.assertEqual(shown["left_out_columns"], ["Sl. No", "Total", "Percentage (2nd Sem)", LEGEND, "Pass - external filled"])
        exams = store.query_records("college_a", "exam", limit=100)
        self.assertEqual(Counter(row["exam_name"] for row in exams), {"SEE (External)": 24, "CIE (Internal)": 24})
        by_key = {(row["student_id"], row["course_code"], row["exam_name"]): row for row in exams}
        self.assertEqual(by_key[("1XX25MB002", "MB106", "CIE (Internal)")]["marks_obtained"], 42 + 11)
        self.assertEqual(by_key[("1XX25MB002", "MB106", "CIE (Internal)")]["course_name"], "Law")

    async def test_two_internal_assessments_are_two_exams_never_one_overwriting_the_other(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        for sheet, mark in (("IA 1", 10), ("IA 2", 20)):
            content = build_xlsx({sheet: _internal_assessment(mark)})
            job = await service.process("college_a", service.upload("college_a", "staff-1", file_name=f"{sheet}.xlsx", content=content, content_type=XLSX)["job_id"])
            [shown], job = await self._review(store, service, job)
            self.assertEqual(shown["exam_names"], [sheet])
            self.assertEqual((job["report"]["import"]["inserted"], job["report"]["import"]["updated"]), (12, 0))
        exams = store.query_records("college_a", "exam", limit=100)
        self.assertEqual(Counter((row["exam_name"], row["marks_obtained"]) for row in exams), {("IA 1", 10): 12, ("IA 2", 20): 12})

    async def test_assessments_on_sheets_named_after_the_class_take_the_exam_from_the_title(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        for name, title, mark in (("ia1", "FIRST INTERNAL ASSESSMENT MARKS", 10), ("ia2", "SECOND INTERNAL ASSESSMENT MARKS", 20)):
            content = build_xlsx({"2nd Sem": _internal_assessment(mark, title)})
            job = await service.process("college_a", service.upload("college_a", "staff-1", file_name=f"{name}.xlsx", content=content, content_type=XLSX)["job_id"])
            [shown], job = await self._review(store, service, job)
            self.assertEqual(shown["exam_names"], [title.removesuffix(" MARKS")])
            self.assertEqual((job["report"]["import"]["inserted"], job["report"]["import"]["updated"]), (12, 0))
        exams = store.query_records("college_a", "exam", limit=100)
        self.assertEqual(Counter((row["exam_name"], row["marks_obtained"]) for row in exams), {("FIRST INTERNAL ASSESSMENT", 10): 12, ("SECOND INTERNAL ASSESSMENT", 20): 12})

    async def test_marks_that_would_replace_stored_marks_wait_for_a_decision(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())

        async def upload(marks):
            job = await service.process("college_a", service.upload("college_a", "staff-1", file_name="marks.csv", content=_csv(marks), content_type="text/csv")["job_id"])
            [review] = [item for item in store.list_review_items("college_a", job_id=job["job_id"]) if item["kind"] == "mapping" and item["status"] == "pending"]
            self.assertEqual(review["payload"]["reshaped_marks"][0]["exam_names"], ["Marks"])
            return await service.apply_mapping("college_a", job["job_id"], mapping=review["payload"]["proposed_mapping"], entity="exam", approved_by="staff-1")

        def stored():
            return {(row["student_id"], row["course_code"]): row["marks_obtained"] for row in store.query_records("college_a", "exam", limit=100)}

        self.assertEqual((await upload({}))["report"]["import"]["inserted"], 12)
        # The same marks again: nothing to ask, nothing changes.
        again = await upload({})
        self.assertEqual((again["status"], again["report"]["import"]["unchanged"], again["report"]["import"]["updated"]), (JOB_IMPORTED, 12, 0))
        # Other marks under the same exam (another test, or a correction): each changed row waits for a person.
        changed = {(1, "MB101"): 15, (2, "MB103"): 18}
        for decision, expected in (("approved", 10), ("rejected", None)):
            job = await upload(changed)
            self.assertEqual((job["status"], job["stage"]), (JOB_NEEDS_REVIEW, "duplicate_review"))
            self.assertEqual(job["report"]["normalization"]["planned_actions"], {"update": 10, "replaces_stored_marks": 2})
            self.assertIn("2 rows would replace marks already stored for exam Marks", job["report"]["normalization"]["warnings"][-1])
            self.assertEqual(stored()[("1XX25MB001", "MB101")], 10)
            items = [item for item in store.list_review_items("college_a", job_id=job["job_id"], status="pending") if item["kind"] == "duplicate"]
            self.assertEqual(len(items), 2)
            evidence = next(item["payload"] for item in items if item["payload"]["left_locator"] == "csv;row=2;col=C")
            self.assertEqual((evidence["kind"], evidence["right_locator"]), ("conflicting_key", None))
            self.assertEqual((evidence["evidence"]["stored"], evidence["evidence"]["new"]), ({"marks_obtained": 10}, {"marks_obtained": 15}))
            for item in items:
                job = service.resolve_review("college_a", item["review_id"], decision=decision, resolved_by="staff-1")
            self.assertEqual(job["status"], JOB_IMPORTED)
            # Approving keeps the stored marks; rejecting ("use this row") replaces them.
            marks = stored()
            self.assertEqual((marks[("1XX25MB001", "MB101")], marks[("1XX25MB002", "MB103")]), (10, 10) if expected else (15, 18))
            self.assertEqual(marks[("1XX25MB003", "MB102")], 10)
        self.assertEqual(store.count_records("college_a", "exam"), 12)

    async def test_the_row_limit_stages_whole_students_and_the_review_counts_what_was_staged(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry(), max_rows=40)
        content = build_xlsx({"2nd Sem": RESULT_WORKBOOK}, merges={"2nd Sem": RESULT_MERGES})
        job = await service.process("college_a", service.upload("college_a", "staff-1", file_name="result.xlsx", content=content, content_type=XLSX)["job_id"])
        # 4 students x 12 exam rows = 48, cut at 40: the fourth student would be cut part-way, so only three are staged.
        self.assertEqual(job["row_count"], 36)
        self.assertTrue(job["report"]["selected_table"]["truncated"])
        [shown], job = await self._review(store, service, job)
        self.assertEqual((shown["exam_rows"], shown["staged_rows"], shown["truncated"]), (48, 36, True))
        self.assertEqual(Counter(row["student_id"] for row in store.query_records("college_a", "exam", limit=100)), {f"1XX25MB00{index}": 12 for index in range(1, 4)})

    async def test_sheets_that_only_look_like_marks_go_to_the_normal_review_unreshaped(self):
        store = InstitutionDataStore(":memory:")
        service = IngestionService(store=store, objects=InMemoryObjectStore(), parsers=ParserRegistry())
        attendance = [["ATTENDANCE FOR THE MONTH OF AUGUST"], ["Sl No", "USN", "Name", "HRM (MBA201)", "FM (MBA202)", "MM (MBA203)", "Total", "Percentage"]] + [
            [index, f"1XX25MB00{index}", f"Test Student {index}", 20 - index, 18, 22, 60 - index, 85.7] for index in range(1, 7)]
        fees = [["USN", "Name", "Tuition Fee (AY2025)", "Hostel Fee (AY2025)", "Transport Fee (AY2025)", "Total"]] + [
            [f"1XX25MB00{index}", f"Test Student {index}", 100000, 60000, 15000, 175000] for index in range(1, 7)]
        # A subject merged over CIE, SEE and Total: the sub-headers are not read as the subject's columns.
        sub_columns = [["RESULT SHEET 2ND SEM"], ["USN", "Name", "HRM (MBA201)", None, None, "FM (MBA202)", None, None, "MM (MBA203)", None, None],
                       [None, None, "CIE", "SEE", "Total", "CIE", "SEE", "Total", "CIE", "SEE", "Total"]] + [
            [f"1XX25MB00{index}", f"Test Student {index}", 40, 50, 90, 41, 51, 92, 42, 52, 94] for index in range(1, 5)]
        for sheet, grid, merges in (("Aug", attendance, None), ("Fees", fees, None), ("2nd Sem", sub_columns, {"2nd Sem": ["C2:E2", "F2:H2", "I2:K2"]})):
            content = build_xlsx({sheet: grid}, merges=merges)
            job = await service.process("college_a", service.upload("college_a", "staff-1", file_name=f"{sheet}.xlsx", content=content, content_type=XLSX)["job_id"])
            self.assertNotIn("reshaped_marks", job["report"], sheet)
            self.assertNotEqual(job["entity"], "exam", sheet)
            self.assertEqual(job["status"], JOB_NEEDS_REVIEW, sheet)
        self.assertEqual(store.count_records("college_a", "exam"), 0)


if __name__ == "__main__":
    unittest.main()
