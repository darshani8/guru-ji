import unittest

from app.institution_data.models import CanonicalRecord
from app.normalization.canonical import ATTENDANCE, CANONICAL_ENTITIES, STUDENT
from app.normalization.cleaning import clean_record, normalize_date, normalize_person_name, normalize_phone, normalize_program, normalize_semester
from app.normalization import deduplication
from app.normalization.deduplication import find_duplicates
from app.normalization.mapping import MappingEngine, apply_mapping, header_signature
from app.normalization.validation import identifier_pattern, ocr_suspicion, validate_record


class _JsonModel:
    provider_id = "fake"
    model_id = "fake"

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_tokens: int = 800) -> str:
        self.prompts.append(prompt)
        return self.reply


class MappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_student_headers_map_with_confidence_and_review(self):
        engine = MappingEngine()
        headers = ["Student Name", "USN", "Course", "Sem", "Phone", "Email ID", "Father Name", "DOB", "Remarks"]
        samples = {"USN": ["1MS23MBA001"], "Phone": ["9876543210"], "Email ID": ["a@b.com"], "DOB": ["12/05/2003"]}
        proposal = await engine.propose(headers, samples)
        self.assertEqual(proposal.entity, "student")
        mapped = proposal.mapped()
        self.assertEqual(mapped["USN"], "student_id")
        self.assertEqual(mapped["Course"], "program")
        self.assertEqual(mapped["Father Name"], "guardian_name")
        self.assertEqual(mapped["DOB"], "date_of_birth")
        self.assertEqual(proposal.unmapped(), ("Remarks",))
        self.assertEqual(proposal.missing_required(), ())

    async def test_abbreviated_and_conflicting_headers_go_to_review(self):
        engine = MappingEngine()
        proposal = await engine.propose(["Name", "ID", "Prog", "Mobile", "Mobile No"], {"Mobile": ["9876543210"], "Mobile No": ["9876543211"]}, entity_hint="student")
        review = {item.source_header for item in proposal.review_required()}
        self.assertIn("Prog", review)
        self.assertTrue({"Mobile", "Mobile No"} & review, "two headers claiming phone must not both auto-apply")
        self.assertIn("Prog", [item.source_header for item in proposal.mappings if item.method == "heuristic"])

    async def test_entity_detection_for_attendance_fee_and_faculty(self):
        engine = MappingEngine()
        attendance = await engine.propose(["USN", "Subject Code", "Total Classes", "Attended", "Attendance %", "Month"])
        self.assertEqual(attendance.entity, "attendance")
        self.assertEqual(attendance.mapped()["Total Classes"], "classes_held")
        fee = await engine.propose(["USN", "Fee Type", "Total Fee", "Paid", "Balance", "Due Date"])
        self.assertEqual(fee.entity, "fee")
        faculty = await engine.propose(["Employee ID", "Faculty Name", "Designation", "Department", "Qualification"])
        self.assertEqual(faculty.entity, "faculty")

    async def test_model_suggestions_are_validated_and_never_auto_applied(self):
        model = _JsonModel('{"Prog": {"field": "program", "confidence": 0.99}, "Remarks": {"field": "made_up_field", "confidence": 0.9}}')
        engine = MappingEngine(model=model)
        proposal = await engine.propose(["Name", "ID", "Prog", "Remarks"], entity_hint="student")
        prog = next(item for item in proposal.mappings if item.source_header == "Prog")
        self.assertEqual(prog.canonical_field, "program")
        self.assertEqual(prog.method, "model_suggestion")
        self.assertLess(prog.confidence, engine.threshold)
        remarks = next(item for item in proposal.mappings if item.source_header == "Remarks")
        self.assertIsNone(remarks.canonical_field)
        self.assertTrue(model.prompts and "made_up_field" not in "".join(model.prompts))

    async def test_saved_profile_short_circuits_mapping(self):
        engine = MappingEngine()
        proposal = await engine.propose(["A", "B"], entity_hint="student", saved_profile={"A": "student_id", "B": "name", "C": "phone"})
        self.assertTrue(proposal.profile_applied)
        self.assertEqual(proposal.mapped(), {"A": "student_id", "B": "name"})
        self.assertEqual(header_signature(["B", "a "]), "a|b")
        # The profile is matched by normalised header, like the signature that found it.
        proposal = await engine.propose(["STUDENT_ID", " Name ", "Other"], entity_hint="student", saved_profile={"Student ID": "student_id", "name": "name", "Other": "not_a_field"})
        self.assertEqual(proposal.mapped(), {"STUDENT_ID": "student_id", " Name ": "name"})
        self.assertEqual(proposal.unmapped(), ("Other",))
        self.assertEqual(proposal.missing_required(), ())

    async def test_similar_headers_never_share_a_field_so_the_proposal_can_be_approved_as_shown(self):
        engine = MappingEngine()
        for headers in (
            ["Name", "USN", "Email", "Email ID", "Personal Email"],
            ["Name", "USN", "Phone", "Mobile", "Contact No"],
            ["Name", "USN", "Father Name", "Mother Name"],
            ["Student Name", "Name", "USN", "Course", "Program"],
            ["Name", "USN", "Address", "Permanent Address"],
        ):
            proposal = await engine.propose(headers, entity_hint="student")
            targets = [item.canonical_field for item in proposal.mappings if item.canonical_field]
            self.assertEqual(len(targets), len(set(targets)), f"{headers} proposes one field twice: {targets}")
        # The weaker header falls back to a free field once; a second one finds none left.
        proposal = await engine.propose(["Name", "USN", "Phone", "Mobile", "Contact No"], {"Phone": ["9876543210"], "Mobile": ["9876543211"], "Contact No": ["9876543212"]}, entity_hint="student")
        by_header = {item.source_header: item for item in proposal.mappings}
        self.assertEqual(by_header["Phone"].canonical_field, "phone")
        self.assertEqual(by_header["Mobile"].canonical_field, "guardian_phone")
        contact = by_header["Contact No"]
        self.assertIsNone(contact.canonical_field)
        self.assertEqual(contact.method, "conflict")
        self.assertIn("already mapped from Phone", contact.reason)
        self.assertEqual(contact.alternatives[0][0], "phone")
        # A header left unmapped by a conflict is still shown to the reviewer.
        self.assertEqual({item.source_header for item in proposal.review_required()}, {"Mobile", "Contact No"})
        self.assertIn("Contact No", proposal.unmapped())

    async def test_model_suggestions_cannot_take_a_field_another_header_holds(self):
        model = _JsonModel('{"Prog": {"field": "name", "confidence": 0.99}}')
        proposal = await MappingEngine(model=model).propose(["Name", "ID", "Prog"], entity_hint="student")
        targets = [item.canonical_field for item in proposal.mappings if item.canonical_field]
        self.assertEqual(len(targets), len(set(targets)), targets)
        self.assertEqual(proposal.mapped()["Name"], "name")

    async def test_profile_headers_that_normalise_alike_do_not_share_a_field(self):
        engine = MappingEngine()
        # The reviewer named one of the look-alikes: their choice stands and needs no new review.
        proposal = await engine.propose(["USN", "Name", "Mobile No", "Mobile No."], entity_hint="student", saved_profile={"USN": "student_id", "Name": "name", "Mobile No.": "phone"})
        self.assertEqual(proposal.mapped(), {"USN": "student_id", "Name": "name", "Mobile No.": "phone"})
        self.assertEqual(proposal.review_required(), ())
        # Neither was named exactly: they cannot both take the remembered field.
        proposal = await engine.propose(["USN", "Name", "Sem", "Sem."], entity_hint="student", saved_profile={"USN": "student_id", "Name": "name", "SEM": "semester"})
        targets = [item.canonical_field for item in proposal.mappings if item.canonical_field]
        self.assertEqual(targets.count("semester"), 1)
        self.assertIn("Sem.", {item.source_header for item in proposal.review_required()})

    def test_apply_mapping_preserves_unmapped_columns_and_combines_names(self):
        canonical, extras = apply_mapping(STUDENT, {"First": "first_name", "Last": "last_name", "ID": "student_id"}, {"First": "Ravi", "Last": "Kumar", "ID": "X1", "Hobby": "chess", "Blank": ""})
        self.assertEqual(canonical["name"], "Ravi Kumar")
        self.assertEqual(extras, {"Hobby": "chess"})


class CleaningAndValidationTests(unittest.TestCase):
    def test_safe_normalizations(self):
        self.assertEqual(normalize_person_name("RAVI  KUMAR")[0], "Ravi Kumar")
        self.assertEqual(normalize_person_name("Ravi Kumar")[0], "Ravi Kumar")
        self.assertEqual(normalize_program("Master of Business Administration")[0], "MBA")
        self.assertEqual(normalize_program("m.b.a")[0], "MBA")
        self.assertEqual(normalize_semester("Sem III")[0], 3)
        self.assertEqual(normalize_semester("third")[0], 3)
        self.assertIsNone(normalize_semester("99")[0])
        self.assertEqual(normalize_phone("98765 43210")[0], "9876543210")
        self.assertEqual(normalize_phone("+91 98765-43210")[0], "+919876543210")
        self.assertEqual(normalize_date("12/05/2003"), ("2003-05-12", True, True))
        self.assertEqual(normalize_date("2003-05-12"), ("2003-05-12", False, False))
        self.assertEqual(normalize_date("15 Jan 2024"), ("2024-01-15", True, False))
        self.assertEqual(normalize_date(45000)[0], "2023-03-15")
        self.assertIsNone(normalize_date("not a date")[0])

    def test_clean_record_derives_attendance_and_fee_fields_without_inventing_values(self):
        cleaned, notes, issues = clean_record(ATTENDANCE, {"student_id": " mba001 ", "classes_held": "20", "classes_absent": "5"})
        self.assertEqual(cleaned["classes_attended"], 15)
        self.assertEqual(cleaned["attendance_percent"], 75.0)
        self.assertIn("attendance_percent:derived_from_counts", notes)
        cleaned, _, issues = clean_record(STUDENT, {"student_id": "X", "name": "A", "phone": "N/A", "semester": "abc"})
        self.assertIsNone(cleaned["phone"])
        self.assertIsNone(cleaned["semester"])
        self.assertEqual([item["code"] for item in issues], ["invalid_semester"])

    def test_validation_severity_and_ocr_suspicion(self):
        issues = validate_record(STUDENT, {"student_id": "", "name": "", "phone": "12", "email": "bad"})
        codes = {item["code"]: item["severity"] for item in issues}
        self.assertEqual(codes["missing_required"], "error")
        self.assertEqual(codes["invalid_phone"], "warning")
        self.assertEqual(codes["invalid_email"], "warning")
        pattern = identifier_pattern(["1MS23MBA001", "1MS23MBA002", "1MS23MBA003", "IMS23MBA00I"])
        self.assertEqual(pattern, "9AA99AAA999")
        suspicion = ocr_suspicion("IMS23MBA00I", pattern)
        self.assertEqual(suspicion["suggested"], "1MS23MBA001")
        issues = validate_record(ATTENDANCE, {"student_id": "X", "classes_held": 10, "classes_attended": 12})
        self.assertIn("attended_exceeds_held", [item["code"] for item in issues])
        self.assertIn("low_ocr_confidence", [item["code"] for item in validate_record(STUDENT, {"student_id": "X1", "name": "A"}, ocr=True, ocr_confidence=0.5)])

    def test_canonical_model_is_consistent(self):
        for entity in CANONICAL_ENTITIES.values():
            self.assertTrue(entity.required_fields())
            for key in entity.natural_key:
                entity.field(key)


class DeduplicationTests(unittest.TestCase):
    def test_deterministic_keys_then_fuzzy_person_matching(self):
        records = [
            CanonicalRecord("student", {"student_id": "MBA001", "name": "Ravi Kumar", "phone": "9876543210"}),
            CanonicalRecord("student", {"student_id": "MBA001", "name": "Ravi Kumar", "phone": "9876543210"}),
            CanonicalRecord("student", {"student_id": "MBA001", "name": "Ravi Kumar", "phone": "1111111111"}),
            CanonicalRecord("student", {"student_id": "MBA009", "name": "Ravi Kumar", "phone": "9876543210", "date_of_birth": "2003-05-12"}),
            CanonicalRecord("student", {"student_id": "MBA010", "name": "Ravi Kumar", "phone": "2222222222"}),
        ]
        existing = {"mba002": {"name": "Ravi Kumar", "date_of_birth": "2003-05-12", "phone": "9876543210"}}
        candidates, actions, _ = find_duplicates(records, existing)
        self.assertEqual(actions[0], "insert")
        self.assertEqual(actions[1], "duplicate_in_batch")
        self.assertEqual(actions[2], "conflict_in_batch")
        kinds = [item.kind for item in candidates]
        self.assertIn("exact_key", kinds)
        self.assertIn("conflicting_key", kinds)
        probable = [item for item in candidates if item.kind == "probable_person"]
        self.assertTrue(any(item.record_key == "mba002" for item in probable), "same person under a different id must be flagged against existing data")
        self.assertFalse(any(item.evidence.get("existing_record_key") == "mba002" and "MBA010" in item.left_locator for item in probable))

    def test_person_matching_is_blocked_and_needs_corroboration(self):
        records = [
            CanonicalRecord("student", {"student_id": "S1", "name": "Ravi Kumar", "phone": "9876543210"}),
            CanonicalRecord("student", {"student_id": "S2", "name": "Ravi Kumar", "phone": "1111111111"}),  # same name, nothing corroborates
            CanonicalRecord("student", {"student_id": "S3", "name": "Ravi Kumaar", "email": "RAVI@x.com"}),  # email block
            CanonicalRecord("student", {"student_id": "S4", "name": "Ravi Kumar", "date_of_birth": "2003-05-12"}),  # dob block
            CanonicalRecord("student", {"student_id": "S5", "name": "Someone Else", "phone": "9876543210"}),  # phone block, name too different
        ]
        existing = {"e1": {"name": "Ravi Kumar", "email": "ravi@x.com"}, "e2": {"name": "Ravi Kumar", "date_of_birth": "2003-05-12", "phone": "9876543210"}}
        calls: list[tuple[str, str]] = []
        original = deduplication._name_similarity

        def counting(left, right):
            calls.append((str(left), str(right)))
            return original(left, right)

        deduplication._name_similarity = counting
        try:
            candidates, actions, warnings = find_duplicates(records, existing)
        finally:
            deduplication._name_similarity = original
        self.assertEqual(warnings, [])
        pairs = {(item.left_locator, item.record_key) for item in candidates if item.kind == "probable_person"}
        self.assertEqual(pairs, {("row=1", "e2"), ("row=3", "e1"), ("row=4", "e2")})
        # Name similarity ran only for pairs that share a corroborating value.
        self.assertTrue(all(len(pair) == 2 for pair in calls))
        self.assertNotIn(("Ravi Kumar", "Ravi Kumar"), [(l, r) for l, r in calls if l == r and "Someone" in r])
        self.assertLessEqual(len(calls), 5)
        self.assertEqual(actions, {0: "insert", 1: "insert", 2: "insert", 3: "insert", 4: "insert"})

    def test_oversized_batches_skip_the_pairwise_pass_with_a_warning(self):
        records = [
            CanonicalRecord("student", {"student_id": f"S{i}", "name": "Ravi Kumar", "phone": "9876543210"})
            for i in range(3)
        ]
        existing = {"e1": {"name": "Ravi Kumar", "phone": "9876543210"}}
        original = deduplication.MAX_PAIRWISE_ROWS
        deduplication.MAX_PAIRWISE_ROWS = 2
        try:
            candidates, _, warnings = find_duplicates(records, existing)
        finally:
            deduplication.MAX_PAIRWISE_ROWS = original
        self.assertEqual(len(warnings), 1)
        self.assertIn("probable_person_check_skipped_in_batch", warnings[0])
        probable = [item for item in candidates if item.kind == "probable_person"]
        self.assertTrue(all(item.record_key == "e1" for item in probable), "rows are still checked against existing records")
        self.assertEqual(len(probable), 3)
        candidates, _, warnings = find_duplicates(records, existing)
        self.assertEqual(warnings, [])
        self.assertEqual(len([item for item in candidates if item.kind == "probable_person"]), 3 + 3)


    def test_a_row_updating_a_record_on_file_is_never_asked_about_as_a_new_identity(self):
        # Two students already on file share a name and phone; an update to one of them creates no identity.
        one = CanonicalRecord("student", {"student_id": "S1", "name": "Ravi Kumar", "phone": "9876543210", "semester": 2})
        other = CanonicalRecord("student", {"student_id": "S2", "name": "Ravi Kumar", "phone": "9876543210", "semester": 1})
        existing = {one.record_key: {"name": "Ravi Kumar", "phone": "9876543210"}, other.record_key: {"name": "Ravi Kumar", "phone": "9876543210"}}
        candidates, actions, _ = find_duplicates([one], existing)
        self.assertEqual(actions, {0: "update"})
        self.assertEqual([item for item in candidates if item.kind == "probable_person"], [])
        # Two updates in one file are separate records already: nothing to ask either.
        candidates, actions, _ = find_duplicates([one, other], existing)
        self.assertEqual(actions, {0: "update", 1: "update"})
        self.assertEqual([item for item in candidates if item.kind == "probable_person"], [])
        # A new identifier for the same person is still asked about.
        new = CanonicalRecord("student", {"student_id": "S9", "name": "Ravi Kumar", "phone": "9876543210"})
        candidates, _, _ = find_duplicates([new], existing)
        self.assertEqual({item.record_key for item in candidates if item.kind == "probable_person"}, {one.record_key, other.record_key})


class DegenerateBlockTests(unittest.TestCase):
    def test_values_shared_by_many_people_neither_slow_nor_flag_matching(self):
        import time

        from app.institution_data.models import CanonicalRecord
        from app.normalization.deduplication import MAX_BLOCK_SIZE, find_duplicates

        # A whole roster sharing the college landline, with near-identical names: not duplicates.
        rows = [CanonicalRecord("student", {"student_id": f"S{i:05d}", "name": f"Student {i} Kumar", "phone": "08012345678", "program": "MBA"}) for i in range(MAX_BLOCK_SIZE + 300)]
        rows.append(CanonicalRecord("student", {"student_id": "X1", "name": "Asha Rao", "phone": "9876543210"}))
        rows.append(CanonicalRecord("student", {"student_id": "X2", "name": "Asha  Rao", "phone": "9876543210"}))
        existing = {f"E{i}": {"name": f"Student {i} Kumar", "phone": "08012345678"} for i in range(MAX_BLOCK_SIZE + 100)}
        started = time.perf_counter()
        candidates, actions, warnings = find_duplicates(rows, existing)
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual([(c.kind, c.record_key) for c in candidates], [("probable_person", "x2")] if candidates and candidates[0].record_key == "x2" else [("probable_person", candidates[0].record_key)] if candidates else [])
        self.assertEqual(len(candidates), 1, "only the pair sharing a private phone is a probable duplicate")
        self.assertEqual(candidates[0].evidence.get("phone"), "match")
        self.assertTrue(any(warning.startswith("shared_values_ignored") for warning in warnings), warnings)
        self.assertEqual(sum(1 for action in actions.values() if action == "insert"), len(rows))


if __name__ == "__main__":
    unittest.main()
