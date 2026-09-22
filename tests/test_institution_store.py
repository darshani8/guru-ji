import unittest

from app.institution_data.models import CanonicalRecord, RecordLineage
from app.institution_data.store import InstitutionDataStore


def _students(n: int, program: str = "MBA"):
    return [CanonicalRecord("student", {"student_id": f"{program}00{i}", "name": f"Student {i}", "program": program, "semester": 1, "phone": "9876543210"}, lineage=RecordLineage(source_file_name="students.xlsx", source_locator=f"sheet=MBA;row={i + 1}")) for i in range(1, n + 1)]


class InstitutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = InstitutionDataStore(":memory:")
        self.store.upsert_institution("college_a", "College A", location="Bengaluru")

    def test_upsert_classifies_new_updated_unchanged_and_keeps_lineage(self):
        first = self.store.upsert_records("college_a", _students(3))
        self.assertEqual((first.inserted, first.updated, first.unchanged), (3, 0, 0))
        again = self.store.upsert_records("college_a", _students(3))
        self.assertEqual((again.inserted, again.updated, again.unchanged), (0, 0, 3))
        changed = _students(3)
        changed[0].fields["semester"] = 2
        third = self.store.upsert_records("college_a", changed)
        self.assertEqual((third.inserted, third.updated, third.unchanged), (0, 1, 2))
        record = self.store.get_record("college_a", "student", "mba001")
        self.assertEqual(record["semester"], 2)
        self.assertEqual(record["lineage"]["source_locator"], "sheet=MBA;row=2")

    def test_records_with_blocking_issues_or_missing_keys_are_skipped(self):
        bad = CanonicalRecord("student", {"student_id": "", "name": "Nobody"})
        blocked = CanonicalRecord("student", {"student_id": "X1", "name": "Blocked"}, issues=({"severity": "error", "code": "missing_required"},))
        summary = self.store.upsert_records("college_a", [bad, blocked, *_students(1)])
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(summary.skipped, 2)
        self.assertEqual(summary.skipped_reasons, {"missing_natural_key": 1, "blocking_issues": 1})

    def test_tenant_isolation_in_every_query_path(self):
        self.store.upsert_records("college_a", _students(2))
        self.store.upsert_records("college_b", _students(1, "BCA"))
        self.assertEqual(self.store.count_records("college_a", "student"), 2)
        self.assertEqual(self.store.count_records("college_b", "student"), 1)
        self.assertIsNone(self.store.get_record("college_b", "student", "mba001"))
        self.assertEqual(self.store.query_records("college_b", "student"), self.store.query_records("college_b", "student", {"program": "bca"}))
        self.assertEqual(self.store.entity_counts("college_b")["student"], 1)

    def test_filters_reject_unknown_columns_and_support_operators(self):
        self.store.upsert_records("college_a", _students(3))
        with self.assertRaises(KeyError):
            self.store.query_records("college_a", "student", {"password": "x"})
        with self.assertRaises(ValueError):
            self.store.query_records("college_a", "student", {"name; drop table": "x"})
        self.assertEqual(len(self.store.query_records("college_a", "student", {"name__contains": "student 2"})), 1)
        self.assertEqual(len(self.store.query_records("college_a", "student", {"student_id__in": ["MBA001", "MBA003"]})), 2)

    def test_attendance_and_fee_rollups(self):
        self.store.upsert_records("college_a", _students(2))
        self.store.upsert_records("college_a", [
            CanonicalRecord("attendance", {"student_id": "MBA001", "course_code": "C1", "period": "Aug", "classes_held": 20, "classes_attended": 10}),
            CanonicalRecord("attendance", {"student_id": "MBA001", "course_code": "C2", "period": "Aug", "classes_held": 10, "classes_attended": 10}),
            CanonicalRecord("attendance", {"student_id": "MBA002", "course_code": "C1", "period": "Aug", "attendance_percent": 91.5}),
        ])
        rollup = {item["student_id"]: item for item in self.store.attendance_rollup("college_a", program="MBA")}
        self.assertAlmostEqual(rollup["MBA001"]["attendance_percent"], 66.67, places=2)
        self.assertEqual(rollup["MBA001"]["name"], "Student 1")
        self.assertEqual(rollup["MBA002"]["attendance_percent"], 91.5)
        self.store.upsert_records("college_a", [
            CanonicalRecord("fee", {"student_id": "MBA001", "fee_type": "Tuition", "amount_due": 1000, "amount_paid": 400}),
            CanonicalRecord("fee", {"student_id": "MBA001", "fee_type": "Hostel", "amount_due": 500, "amount_paid": 500, "balance": 0}),
        ])
        fees = self.store.fee_rollup("college_a")
        self.assertEqual(fees[0]["balance"], 600.0)

    def test_update_record_fields_rejects_natural_key_changes(self):
        self.store.upsert_records("college_a", _students(1))
        before, after = self.store.update_record_fields("college_a", "student", "mba001", {"semester": 3}, locator="agent:req-1")
        self.assertEqual((before["semester"], after["semester"]), (1, 3))
        with self.assertRaises(ValueError):
            self.store.update_record_fields("college_a", "student", "mba001", {"student_id": "X"}, locator="agent")

    def test_review_items_approvals_and_background_jobs(self):
        job = self.store.create_job("college_a", job_id="job-1", file_id=None, entity="student", requested_by="u1")
        self.assertEqual(job["status"], "queued")
        ids = self.store.add_review_items("college_a", "job-1", [{"kind": "mapping", "payload": {"x": 1}}])
        self.assertEqual(len(self.store.list_review_items("college_a", job_id="job-1")), 1)
        resolved = self.store.resolve_review_item("college_a", ids[0], status="approved", resolved_by="u1")
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(self.store.list_review_items("college_a", job_id="job-1"), [])
        approval = self.store.create_approval("college_a", approval_id="apr-1", principal_id="u1", tool_name="update_student_record", arguments={"a": 1}, arguments_sha256="abc", reason="r", ttl_seconds=60)
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(self.store.decide_approval("college_a", "apr-1", status="approved", decided_by="u1")["status"], "approved")
        self.store.enqueue_background_job("college_a", job_id="bg-1", job_type="x", payload={"n": 1})
        claimed = self.store.claim_background_jobs()
        self.assertEqual([item["job_id"] for item in claimed], ["bg-1"])
        self.assertEqual(self.store.claim_background_jobs(), [])
        self.store.finish_background_job("bg-1", status="succeeded", result={"ok": True})
        self.assertEqual(self.store.get_background_job("bg-1")["result"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
