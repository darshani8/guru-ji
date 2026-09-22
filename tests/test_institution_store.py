import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.institution_data.models import CanonicalRecord, RecordLineage
from app.institution_data.schema import column_type, postgres_numeric_columns, render_sql_migration
from app.institution_data.store import WRITE_CHUNK_ROWS, InstitutionDataStore
from app.normalization.canonical import FieldType, entity as canonical_entity
from app.persistence.sql_backend import PostgresBackend, SqlBackend, SqliteBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


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

    def test_boolean_filters_bind_as_integers(self):
        # PostgreSQL refuses ``integer = boolean``; filters must bind 1/0 exactly like writes do.
        entity = canonical_entity("faculty")
        _, params = self.store._where(entity, {"is_hod": True})
        self.assertEqual(params, [1])
        _, params = self.store._where(entity, {"is_hod": False})
        self.assertEqual(params, [0])
        self.assertTrue(all(type(value) is int for value in params))
        _, params = self.store._where(entity, {"is_hod__in": [True, False]})
        self.assertEqual(params, [1, 0])
        self.assertTrue(all(type(value) is int for value in params))
        # Text spellings of a boolean and range operators bind as integers too, never through LOWER().
        clause, params = self.store._where(entity, {"is_hod": "true", "is_hod__gte": False})
        self.assertEqual(params, [1, 0])
        self.assertNotIn("LOWER(is_hod)", clause)
        # Non-boolean fields keep their values.
        _, params = self.store._where(canonical_entity("student"), {"semester": 3, "semester__in": [1, 2]})
        self.assertEqual(params, [3, 1, 2])
        self.store.upsert_records("college_a", [
            CanonicalRecord("faculty", {"faculty_id": "F1", "name": "Head", "department": "MBA", "is_hod": True}),
            CanonicalRecord("faculty", {"faculty_id": "F2", "name": "Member", "department": "MBA", "is_hod": False}),
        ])
        self.assertEqual([row["faculty_id"] for row in self.store.query_records("college_a", "faculty", {"is_hod": True})], ["F1"])
        self.assertEqual([row["faculty_id"] for row in self.store.query_records("college_a", "faculty", {"is_hod__in": [False]})], ["F2"])
        self.assertEqual(self.store.count_records("college_a", "faculty", {"is_hod": False}), 1)

    def test_bulk_writes_are_atomic_and_keep_lineage(self):
        commits: list[int] = []
        original_commit = self.store.backend.commit

        def counting_commit() -> None:
            commits.append(1)
            original_commit()

        self.store.backend.commit = counting_commit  # type: ignore[method-assign]
        total = WRITE_CHUNK_ROWS * 2 + 7
        summary = self.store.upsert_records("college_a", _students(total))
        self.assertEqual((summary.inserted, summary.updated, summary.unchanged), (total, 0, 0))
        # Three lookup transactions plus one write transaction: the import lands whole or not at all.
        self.assertEqual(len(commits), 4)
        self.assertEqual(self.store.count_records("college_a", "student"), total)
        last = self.store.get_record("college_a", "student", f"mba00{total}")
        self.assertEqual(last["lineage"]["source_locator"], f"sheet=MBA;row={total + 1}")
        self.assertEqual(last["lineage"]["source_file_name"], "students.xlsx")
        commits.clear()
        staged = [{"row_number": i, "locator": f"row={i}", "raw": {"n": i}, "status": "parsed"} for i in range(1, WRITE_CHUNK_ROWS * 2 + 2)]
        self.assertEqual(self.store.replace_job_records("college_a", "job-1", iter(staged)), len(staged))
        self.assertEqual(len(commits), 1)  # delete and all insert batches in one transaction
        self.assertEqual(len(self.store.job_records("college_a", "job-1", limit=5000)), len(staged))
        self.assertEqual(self.store.replace_job_records("college_a", "job-1", []), 0)
        self.assertEqual(self.store.job_records("college_a", "job-1"), [])

    def test_bulk_write_failure_leaves_nothing_behind(self):
        calls = {"n": 0}
        original = self.store.backend.executemany

        def failing_executemany(sql, rows):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
            return original(sql, rows)

        self.store.backend.executemany = failing_executemany  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            self.store.upsert_records("college_a", _students(WRITE_CHUNK_ROWS + 5))
        # The first batch was rolled back with the failed one, so no row is
        # attributed to an import that reported nothing, and the store still works.
        self.assertEqual(self.store.count_records("college_a", "student"), 0)
        self.assertFalse(self.store.backend.in_transaction)
        self.assertEqual(self.store.upsert_records("college_a", _students(1)).inserted, 1)
        calls["n"] = 0
        staged = [{"row_number": i, "locator": f"row={i}", "raw": {"n": i}, "status": "parsed"} for i in range(1, WRITE_CHUNK_ROWS + 3)]
        self.store.backend.executemany = original  # type: ignore[method-assign]
        self.assertEqual(self.store.replace_job_records("college_a", "job-1", iter(staged)), len(staged))
        self.store.backend.executemany = failing_executemany  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            self.store.replace_job_records("college_a", "job-1", iter(staged[:3] * 400))
        # A failed replacement keeps the previously staged rows intact.
        self.assertEqual(len(self.store.job_records("college_a", "job-1", limit=5000)), len(staged))

    def test_claim_background_job_claims_exactly_one_queued_job(self):
        self.store.enqueue_background_job("college_a", job_id="bg-a", job_type="ingest", payload={"n": 1})
        self.store.enqueue_background_job("college_a", job_id="bg-b", job_type="ingest", payload={"n": 2})
        job = self.store.claim_background_job("bg-b")
        self.assertEqual((job["job_id"], job["status"], job["attempts"]), ("bg-b", "running", 1))
        self.assertIsNotNone(job["started_at"])
        self.assertEqual(job["payload"], {"n": 2})
        # The other job is untouched and still claimable by the polling path.
        self.assertEqual(self.store.get_background_job("bg-a")["status"], "queued")
        self.assertIsNone(self.store.claim_background_job("bg-b"), "a running job cannot be claimed twice")
        self.assertIsNone(self.store.claim_background_job("bg-missing"))
        self.store.finish_background_job("bg-b", status="succeeded")
        self.assertIsNone(self.store.claim_background_job("bg-b"), "a finished job cannot be claimed")
        self.assertEqual([item["job_id"] for item in self.store.claim_background_jobs()], ["bg-a"])

    def test_requeue_stale_background_jobs(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        for job_id in ("bg-stale", "bg-fresh", "bg-exhausted", "bg-no-start"):
            self.store.enqueue_background_job("college_a", job_id=job_id, job_type="ingest", payload={})
        for job_id in ("bg-stale", "bg-fresh", "bg-exhausted", "bg-no-start"):
            self.assertIsNotNone(self.store.claim_background_job(job_id))
        with self.store.backend.transaction():
            # Staleness is judged by the last heartbeat (falling back to the claim, then the enqueue time).
            self.store.backend.execute("UPDATE background_jobs SET started_at = ?, heartbeat_at = ? WHERE job_id IN (?, ?)", (old, old, "bg-stale", "bg-exhausted"))
            self.store.backend.execute("UPDATE background_jobs SET attempts = 3 WHERE job_id = ?", ("bg-exhausted",))
            self.store.backend.execute("UPDATE background_jobs SET started_at = NULL, heartbeat_at = NULL, created_at = ? WHERE job_id = ?", (old, "bg-no-start"))
        self.assertEqual(self.store.requeue_stale_background_jobs(older_than_seconds=3600), [], "claims younger than the window are live")
        # A heartbeat newer than the claim keeps a long-running job alive.
        with self.store.backend.transaction():
            self.store.backend.execute("UPDATE background_jobs SET started_at = ? WHERE job_id = ?", (old, "bg-fresh"))
        self.assertTrue(self.store.heartbeat_background_job("bg-fresh"))
        self.assertFalse(self.store.heartbeat_background_job("bg-missing"))
        requeued = self.store.requeue_stale_background_jobs(older_than_seconds=60, max_attempts=3)
        self.assertEqual(sorted(item["job_id"] for item in requeued), ["bg-no-start", "bg-stale"])
        stale = self.store.get_background_job("bg-stale")
        self.assertEqual((stale["status"], stale["started_at"], stale["attempts"]), ("queued", None, 1))
        self.assertEqual(self.store.get_background_job("bg-fresh")["status"], "running", "a live claim is left alone")
        exhausted = self.store.get_background_job("bg-exhausted")
        self.assertEqual(exhausted["status"], "failed")
        self.assertIn("repeated attempts", exhausted["error"])
        self.assertIsNotNone(exhausted["finished_at"])
        self.assertEqual(self.store.requeue_stale_background_jobs(older_than_seconds=60), [], "requeueing is idempotent")
        self.assertEqual(self.store.claim_background_job("bg-stale")["attempts"], 2)
        # An earlier attempt finishing late cannot overwrite the re-claimed run; the current attempt can.
        self.assertFalse(self.store.finish_background_job("bg-stale", status="succeeded", attempt=1))
        self.assertEqual(self.store.get_background_job("bg-stale")["status"], "running")
        self.assertTrue(self.store.finish_background_job("bg-stale", status="succeeded", attempt=2))
        self.assertEqual(self.store.get_background_job("bg-stale")["status"], "succeeded")
        self.assertTrue(self.store.requeue_background_job("bg-fresh"))
        self.assertEqual(self.store.get_background_job("bg-fresh")["status"], "queued")


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _check(self, sql, params):
        module = self.connection.module
        if self.connection.closed:
            raise module.OperationalError("the connection is closed")
        if self.connection.kill_on_next:
            self.connection.kill_on_next = False
            self.connection.closed = True
            raise module.OperationalError("terminating connection due to administrator command")
        if self.connection.cancel_on_next:
            self.connection.cancel_on_next = False
            raise module.OperationalError("canceling statement due to statement timeout")
        if self.connection.fail_on_next:
            self.connection.fail_on_next = False
            raise module.DatabaseError("division by zero")
        self.connection.statements.append((sql, tuple(params)))

    def execute(self, sql, params=()):
        self._check(sql, params)

    def executemany(self, sql, rows):
        self._check(sql, ("many", len(rows)))

    def fetchall(self):
        return [{"one": 1}]

    def fetchone(self):
        return {"one": 1}


class _FakeConnection:
    def __init__(self, module):
        self.module = module
        self.closed = False
        self.broken = False
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.kill_on_next = False
        self.cancel_on_next = False
        self.fail_on_next = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        if self.closed:
            raise self.module.OperationalError("the connection is closed")
        self.commits += 1

    def rollback(self):
        if self.closed:
            raise self.module.OperationalError("the connection is closed")
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakePsycopg(types.ModuleType):
    def __init__(self):
        super().__init__("psycopg")
        self.OperationalError = type("OperationalError", (Exception,), {})
        self.InterfaceError = type("InterfaceError", (Exception,), {})
        self.DatabaseError = type("DatabaseError", (Exception,), {})
        self.connections: list[_FakeConnection] = []
        self.refuse = False
        self.rows = types.ModuleType("psycopg.rows")
        self.rows.dict_row = object()

    def connect(self, url, row_factory=None, **options):
        self.connect_options = dict(options)
        if self.refuse:
            raise self.OperationalError("connection refused")
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection


class PostgresBackendReconnectTests(unittest.TestCase):
    """Exercise the reconnect logic with a fake driver so no server is needed."""

    def setUp(self):
        self.fake = _FakePsycopg()
        self._saved = {name: sys.modules.get(name) for name in ("psycopg", "psycopg.rows")}
        sys.modules["psycopg"] = self.fake
        sys.modules["psycopg.rows"] = self.fake.rows
        self.backend = PostgresBackend("postgresql://fake/db")

    def tearDown(self):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_statement_outside_transaction_reconnects_and_retries_once(self):
        first = self.fake.connections[0]
        first.kill_on_next = True
        self.assertEqual(self.backend.fetchall("SELECT 1"), [{"one": 1}])
        self.assertEqual(len(self.fake.connections), 2)
        second = self.fake.connections[1]
        self.assertEqual(second.statements, [("SELECT 1", ())])
        self.assertEqual(second.commits, 1, "a read outside a transaction commits so nothing stays idle-in-transaction")
        self.assertTrue(first.closed)
        # An already-closed connection is detected before use, without an error round trip.
        second.closed = True
        self.assertEqual(self.backend.execute("DELETE FROM t WHERE id = ?", (1,)), 1)
        self.assertEqual(len(self.fake.connections), 3)
        self.assertEqual(self.fake.connections[2].statements, [("DELETE FROM t WHERE id = %s", (1,))])
        self.assertEqual(self.fake.connections[2].commits, 1)

    def test_connections_are_opened_with_a_connect_timeout_and_the_client_check(self):
        self.assertEqual(self.fake.connect_options.get("connect_timeout"), 15)
        self.assertEqual(self.fake.connect_options.get("options"), "-c client_connection_check_interval=10s")

    def test_ping_commits_and_recovers(self):
        self.assertTrue(self.backend.ping())
        self.assertEqual(self.fake.connections[0].commits, 1)
        self.fake.connections[0].kill_on_next = True
        self.assertTrue(self.backend.ping())
        self.assertEqual(len(self.fake.connections), 2)
        self.assertEqual(self.fake.connections[1].commits, 1)

    def test_drop_before_any_statement_in_a_tenant_transaction_is_retried(self):
        first = self.fake.connections[0]
        first.kill_on_next = True  # the idle connection was dropped; set_config is the first statement to notice
        with self.backend.transaction(tenant_id="college_a"):
            self.backend.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(len(self.fake.connections), 2)
        second = self.fake.connections[1]
        self.assertEqual([sql for sql, _ in second.statements], ["SELECT set_config('app.institution_id', %s, true)", "INSERT INTO t VALUES (1)"])
        self.assertEqual(second.commits, 1)
        self.assertFalse(self.backend.in_transaction)

    def test_drop_on_the_first_statement_reapplies_the_tenant_and_retries(self):
        first = self.fake.connections[0]
        with self.backend.transaction(tenant_id="college_a"):
            first.kill_on_next = True
            self.backend.execute("INSERT INTO t VALUES (1)")
            self.backend.execute("INSERT INTO t VALUES (2)")
        second = self.fake.connections[1]
        self.assertEqual(
            [sql for sql, _ in second.statements],
            ["SELECT set_config('app.institution_id', %s, true)", "INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"],
        )
        self.assertEqual(second.statements[0][1], ("college_a",))
        self.assertEqual(second.commits, 1)

    def test_failed_statement_outside_transaction_is_rolled_back(self):
        first = self.fake.connections[0]
        first.fail_on_next = True
        with self.assertRaises(self.fake.DatabaseError):
            self.backend.fetchall("SELECT 1/0")
        self.assertEqual(first.rollbacks, 1, "the aborted implicit transaction is rolled back so the next statement works")
        self.assertFalse(first.closed)
        self.assertTrue(self.backend.ping())
        self.assertEqual(len(self.fake.connections), 1)
        # Inside an explicit transaction the block's own rollback handles it.
        first.fail_on_next = True
        with self.assertRaises(self.fake.DatabaseError):
            with self.backend.transaction(tenant_id="college_a"):
                self.backend.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(first.rollbacks, 2)

    def test_failure_inside_transaction_reraises_and_next_transaction_works(self):
        first = self.fake.connections[0]
        with self.assertRaises(self.fake.OperationalError):
            with self.backend.transaction(tenant_id="college_a"):
                self.backend.execute("INSERT INTO t VALUES (1)")
                first.kill_on_next = True  # dropped after work was done: nothing may be replayed
                self.backend.execute("INSERT INTO t VALUES (2)")
        self.assertFalse(self.backend.in_transaction)
        self.assertEqual(len(self.fake.connections), 2, "the connection was reopened for the next caller")
        second = self.fake.connections[1]
        self.assertEqual(second.statements, [], "the failed statement was not replayed on the new connection")
        with self.backend.transaction(tenant_id="college_a"):
            self.backend.execute("INSERT INTO t VALUES (2)")
        self.assertEqual([sql for sql, _ in second.statements], ["SELECT set_config('app.institution_id', %s, true)", "INSERT INTO t VALUES (2)"])
        self.assertEqual(second.commits, 1)

    def test_transaction_failure_while_server_is_down_still_fails_cleanly(self):
        first = self.fake.connections[0]
        first.kill_on_next = True
        self.fake.refuse = True
        with self.assertRaises(self.fake.OperationalError):
            with self.backend.transaction():
                self.backend.execute("INSERT INTO t VALUES (1)")  # reconnect attempt is refused
        self.assertFalse(self.backend.in_transaction)
        self.fake.refuse = False
        self.assertEqual(self.backend.fetchall("SELECT 1"), [{"one": 1}])

    def test_error_on_live_connection_is_not_retried(self):
        first = self.fake.connections[0]
        first.cancel_on_next = True
        with self.assertRaises(self.fake.OperationalError):
            self.backend.fetchall("SELECT pg_sleep(100)")
        self.assertEqual(len(self.fake.connections), 1)
        self.assertFalse(first.closed)

    def test_close_is_final(self):
        self.backend.close()
        self.assertTrue(self.fake.connections[0].closed)
        with self.assertRaises(self.fake.InterfaceError):
            self.backend.ping()
        self.assertEqual(len(self.fake.connections), 1)


class _RecordingBackend(SqlBackend):
    """Pretends to be PostgreSQL and records DDL so the numeric upgrade can be checked without a server."""

    dialect = "postgresql"

    def __init__(self, real_columns):
        super().__init__()
        self.real_columns = list(real_columns)
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        return 0

    def fetchall(self, sql, params=()):
        assert "information_schema.columns" in sql and "data_type = 'real'" in sql
        return [{"table_name": table, "column_name": column} for table, column in self.real_columns]

    def commit(self):
        return None

    def rollback(self):
        return None


class SchemaNumericTypeTests(unittest.TestCase):
    def test_number_and_percent_fields_are_double_precision(self):
        self.assertEqual(column_type(FieldType.NUMBER), "DOUBLE PRECISION")
        self.assertEqual(column_type(FieldType.PERCENT), "DOUBLE PRECISION")
        self.assertEqual(column_type(FieldType.BOOLEAN), "INTEGER")
        wanted = postgres_numeric_columns()
        self.assertIn(("fees", "amount_due"), wanted)
        self.assertIn(("fees", "balance"), wanted)
        self.assertIn(("attendance", "attendance_percent"), wanted)
        self.assertNotIn(("faculty", "is_hod"), wanted)
        self.assertNotIn(("students", "semester"), wanted)
        self.assertNotIn("REAL", render_sql_migration())

    def test_checked_in_migration_matches_generator(self):
        checked_in = (REPO_ROOT / "migrations" / "002_institution_data.sql").read_text(encoding="utf-8")
        self.assertEqual(checked_in, render_sql_migration(), "regenerate migrations/002_institution_data.sql with render_sql_migration()")

    def test_sqlite_accepts_double_precision_and_sums_exactly(self):
        store = InstitutionDataStore(":memory:")
        store.upsert_records("college_a", [CanonicalRecord("fee", {"student_id": "S1", "fee_type": f"T{i}", "amount_due": 87654.32, "amount_paid": 0}) for i in range(20)])
        self.assertEqual(store.fee_rollup("college_a")[0]["amount_due"], round(87654.32 * 20, 2))

    def test_postgres_upgrade_alters_only_stale_real_columns_and_is_idempotent(self):
        backend = _RecordingBackend([("fees", "amount_due"), ("fees", "balance"), ("internet_documents", "match_score"), ("faculty", "is_hod")])
        store = InstitutionDataStore.__new__(InstitutionDataStore)
        store.backend = backend
        store._upgrade_postgres_numeric_columns()
        self.assertEqual(backend.statements, [
            "ALTER TABLE fees ALTER COLUMN amount_due TYPE DOUBLE PRECISION",
            "ALTER TABLE fees ALTER COLUMN balance TYPE DOUBLE PRECISION",
        ])
        backend.real_columns = []
        backend.statements.clear()
        store._upgrade_postgres_numeric_columns()
        self.assertEqual(backend.statements, [], "nothing is altered once every column is already DOUBLE PRECISION")

    def test_sqlite_store_never_runs_the_postgres_upgrade(self):
        backend = SqliteBackend(":memory:")
        calls: list[str] = []
        original = backend.fetchall

        def spy(sql, params=()):
            calls.append(sql)
            return original(sql, params)

        backend.fetchall = spy  # type: ignore[method-assign]
        InstitutionDataStore(backend=backend)
        self.assertFalse(any("information_schema" in sql for sql in calls))


if __name__ == "__main__":
    unittest.main()
