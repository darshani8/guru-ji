"""Exercise the institution data platform against a real PostgreSQL database.

Run with CONTROL_DATABASE_URL (or INSTITUTION_DATABASE_URL) pointing at a
disposable PostgreSQL database. It verifies the placeholder rewrite, upserts,
rollups, ingestion state tables, the intelligence store, and that row-level
security hides rows from a connection that has not set a tenant.
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

from app.institution_data.models import CanonicalRecord
from app.institution_data.store import InstitutionDataStore
from app.internet_intelligence.profile import InstitutionProfile
from app.internet_intelligence.store import IntelligenceStore


def main() -> None:
    url = os.getenv("INSTITUTION_DATABASE_URL") or os.getenv("CONTROL_DATABASE_URL") or ""
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("set INSTITUTION_DATABASE_URL or CONTROL_DATABASE_URL to a PostgreSQL URL")
    institution = f"smoke_{uuid4().hex[:8]}"
    store = InstitutionDataStore(url)
    try:
        assert store.backend_name == "postgresql"
        store.upsert_institution(institution, "Smoke College", location="Bengaluru")
        students = [CanonicalRecord("student", {"student_id": f"S{i}", "name": f"Student {i}", "program": "MBA", "semester": 1}) for i in range(1, 4)]
        assert store.upsert_records(institution, students).inserted == 3
        assert store.upsert_records(institution, students).unchanged == 3
        assert store.count_records(institution, "student", {"name__contains": "student 2"}) == 1
        store.upsert_records(institution, [
            CanonicalRecord("attendance", {"student_id": "S1", "course_code": "C1", "period": "Aug", "classes_held": 20, "classes_attended": 10}),
            CanonicalRecord("fee", {"student_id": "S1", "fee_type": "Tuition", "amount_due": 1000, "amount_paid": 400}),
            CanonicalRecord("exam", {"student_id": "S1", "course_code": "C1", "exam_name": "IA1", "marks_obtained": 30, "max_marks": 50, "result_status": "pass"}),
        ][:1])
        store.upsert_records(institution, [CanonicalRecord("fee", {"student_id": "S1", "fee_type": "Tuition", "amount_due": 1000, "amount_paid": 400})])
        store.upsert_records(institution, [CanonicalRecord("exam", {"student_id": "S1", "course_code": "C1", "exam_name": "IA1", "marks_obtained": 30, "max_marks": 50, "result_status": "pass"})])
        assert store.attendance_rollup(institution, program="MBA")[0]["attendance_percent"] == 50.0
        assert store.fee_rollup(institution)[0]["balance"] == 600.0
        assert store.exam_rollup(institution, program="MBA")[0]["passed"] == 1
        store.create_job(institution, job_id=f"job-{uuid4().hex}", file_id=None, entity="student", requested_by="smoke")
        assert store.list_jobs(institution)
        job_id = f"bg-{uuid4().hex}"
        store.enqueue_background_job(institution, job_id=job_id, job_type="smoke", payload={})
        assert [item["job_id"] for item in store.claim_background_jobs(50) if item["job_id"] == job_id]
        store.finish_background_job(job_id, status="succeeded")
        intel = IntelligenceStore(backend=store.backend)
        intel.save_profile(InstitutionProfile(institution, "Smoke College", "Bengaluru", monitoring_enabled=True), updated_by="smoke")
        assert institution in intel.monitored_institutions(), "scheduler must see monitored profiles without a tenant context"
        import psycopg

        with psycopg.connect(url) as connection:
            hidden = connection.execute("SELECT count(*) FROM students WHERE institution_id = %s", (institution,)).fetchone()[0]
            assert hidden == 0, "row-level security must hide rows when no tenant is set"
            connection.execute("SELECT set_config('app.institution_id', %s, true)", (institution,))
            visible = connection.execute("SELECT count(*) FROM students WHERE institution_id = %s", (institution,)).fetchone()[0]
            assert visible == 3
        for entity in ("student", "attendance", "fee", "exam"):
            store.delete_by_job(institution, entity, "none")
        print("PLATFORM_POSTGRES_SMOKE_OK")
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
