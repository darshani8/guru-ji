import asyncio
import importlib.util
import unittest
from unittest import mock

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

STUDENTS = b"Student Name,USN,Course,Sem,Phone\nRavi Kumar,1MS23MBA001,MBA,1,9876543210\nAsha Rao,1MS23MBA002,MBA,1,9876543211\n"
ATTENDANCE = b"USN,Subject Code,Total Classes,Attended,Month\n1MS23MBA001,MBA101,20,12,Aug\n1MS23MBA002,MBA101,20,19,Aug\n"


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class PlatformRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.principal = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "route-principal", "X-Demo-Role": "principal", "X-Demo-College": "route_college"}
        cls.admin = {**cls.principal, "X-Demo-Role": "institution_admin"}
        cls.student = {**cls.principal, "X-Demo-Principal": "route-student", "X-Demo-Role": "student"}
        cls.client.put("/v1/institutions/route_college", headers=cls.admin, json={"name": "Route College", "location": "Mysuru", "email_domains": ["route.example"]})
        upload = cls.client.post("/v1/ingestion/uploads", headers=cls.principal, files={"file": ("students.csv", STUDENTS, "text/csv")}, data={"entity": "student"})
        assert upload.status_code == 200, upload.text
        cls.job_id = upload.json()["job"]["job_id"]
        cls.client.post("/v1/ingestion/uploads", headers=cls.principal, files={"file": ("attendance.csv", ATTENDANCE, "text/csv")})

    def test_downloading_a_report_whose_file_is_gone_is_410_not_500(self):
        platform = app.state.runtime.platform
        command = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "Create a csv report of MBA students below 75% attendance"})
        self.assertEqual(command.status_code, 200, command.text)
        artifacts = command.json().get("artifacts") or []
        self.assertTrue(artifacts, command.json())
        report_id = artifacts[0].get("report_id") or str(artifacts[0].get("download_path", "")).rsplit("/", 2)[-2]
        path = f"/v1/reports/{report_id}/download"
        self.assertEqual(self.client.get(path, headers=self.principal).status_code, 200)
        record = platform.store.get_report("route_college", report_id)
        self.assertTrue(platform.objects.delete(record["object_key"]))
        gone = self.client.get(path, headers=self.principal)
        self.assertEqual(gone.status_code, 410, gone.text)
        self.assertIn("no longer available", gone.json()["detail"])

    def test_readiness_reports_an_unreachable_store_as_not_ready(self):
        platform = app.state.runtime.platform

        def broken():
            raise RuntimeError("connection refused")

        with mock.patch.object(platform.store, "ping", broken):
            response = self.client.get("/v1/health/ready")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertFalse(body["platform"]["institution_database_ok"])
        self.assertTrue(body["database_ok"])
        self.assertEqual(self.client.get("/v1/health/ready").status_code, 200)

    def test_failed_jobs_can_be_retried_and_finished_jobs_cannot(self):
        platform = app.state.runtime.platform
        done = self.client.post(f"/v1/ingestion/jobs/{self.job_id}/retry", headers=self.principal)
        self.assertEqual(done.status_code, 409, done.text)
        upload = self.client.post("/v1/ingestion/uploads", headers=self.principal, files={"file": ("again.csv", STUDENTS, "text/csv")}, data={"entity": "student"})
        job_id = upload.json()["job"]["job_id"]
        platform.store.update_job("route_college", job_id, status="failed", stage="parsing", error="simulated")
        retried = self.client.post(f"/v1/ingestion/jobs/{job_id}/retry", headers=self.principal)
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(self.client.get(f"/v1/ingestion/jobs/{job_id}", headers=self.principal).json()["job"]["status"], "imported")
        self.assertEqual(self.client.post(f"/v1/ingestion/jobs/{job_id}/retry", headers=self.student).status_code, 403)
        self.assertEqual(self.client.post("/v1/ingestion/jobs/nope/retry", headers=self.principal).status_code, 404)

    def test_ingestion_routes_expose_job_state_and_report(self):
        job = self.client.get(f"/v1/ingestion/jobs/{self.job_id}", headers=self.principal).json()["job"]
        self.assertEqual(job["status"], "imported")
        report = self.client.get(f"/v1/ingestion/jobs/{self.job_id}/report", headers=self.principal).json()
        self.assertEqual(report["import"]["inserted"], 2)
        records = self.client.get(f"/v1/ingestion/jobs/{self.job_id}/records", headers=self.principal).json()["records"]
        self.assertEqual(records[0]["normalized"]["fields"]["student_id"], "1MS23MBA001")
        entities = self.client.get("/v1/ingestion/entities", headers=self.principal).json()["entities"]
        self.assertIn("student", [item["name"] for item in entities])
        self.assertEqual(self.client.post("/v1/ingestion/uploads", headers=self.student, files={"file": ("x.csv", b"a,b", "text/csv")}).status_code, 403)
        self.assertEqual(self.client.post("/v1/ingestion/uploads", headers=self.principal, files={"file": ("x.csv", STUDENTS, "text/csv")}, data={"entity": "unicorn"}).status_code, 422)

    def test_data_access_routes_enforce_capabilities(self):
        summary = self.client.get("/v1/data/summary", headers=self.principal).json()
        self.assertEqual(summary["record_counts"]["student"], 2)
        low = self.client.get("/v1/data/attendance/low?program=MBA&threshold=75", headers=self.principal).json()
        self.assertEqual(low["count"], 1)
        self.assertEqual(self.client.get("/v1/data/students", headers=self.student).status_code, 403)
        self.assertEqual(self.client.get("/v1/data/students?institution_id=other_college", headers=self.principal).status_code, 403)
        student = self.client.get("/v1/data/students/1MS23MBA001", headers=self.principal).json()["student"]
        self.assertEqual(student["phone"], "9876543210")
        faculty_view = self.client.get("/v1/data/students/1MS23MBA001", headers={**self.principal, "X-Demo-Role": "faculty"}).json()["student"]
        self.assertNotIn("phone", faculty_view)

    def test_agent_routes_run_commands_tools_and_approvals(self):
        answer = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "How many MBA students have attendance below 75%?"}).json()
        self.assertEqual(answer["status"], "complete")
        self.assertIn("1 of 2", answer["answer"])
        tools = self.client.get("/v1/agent/tools", headers=self.principal).json()
        self.assertIn("update_student_record", [tool["name"] for tool in tools["tools"]])
        report = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "Create a csv report of MBA students below 75% attendance", "include_data": False}).json()
        download = self.client.get(report["artifacts"][0]["download_path"], headers=self.principal)
        self.assertEqual(download.status_code, 200)
        self.assertIn("1MS23MBA001", download.text)
        self.assertEqual(self.client.get(report["artifacts"][0]["download_path"], headers=self.student).status_code, 403)
        pending = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "Update the section of student 1MS23MBA002 to B"}).json()
        self.assertEqual(pending["status"], "approval_required")
        approval_id = pending["approval"]["approval_id"]
        self.assertEqual(self.client.post(f"/v1/agent/approvals/{approval_id}", headers={**self.principal, "X-Demo-Principal": "someone-else"}, json={"approve": True}).status_code, 403)
        self.assertEqual(self.client.post(f"/v1/agent/approvals/{approval_id}", headers=self.principal, json={"approve": True}).json()["approval"]["status"], "approved")
        done = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "Update the section of student 1MS23MBA002 to B", "approval_id": approval_id}).json()
        self.assertEqual(done["status"], "complete")
        self.assertEqual(self.client.get("/v1/data/students/1MS23MBA002", headers=self.principal).json()["student"]["section"], "B")
        background = self.client.post("/v1/agent/commands", headers=self.principal, json={"command": "How many students are there?", "run_in_background": True}).json()
        self.assertEqual(background["status"], "accepted")
        job = self.client.get(f"/v1/agent/jobs/{background['job_id']}", headers=self.principal).json()["job"]
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(any(item["title"].startswith("Command finished") for item in self.client.get("/v1/notifications", headers=self.principal).json()["notifications"]))
        refused = self.client.post("/v1/agent/commands", headers=self.student, json={"command": "How many students are there?"}).json()
        self.assertEqual(refused["status"], "needs_input")

    def test_document_routes(self):
        upload = self.client.post("/v1/documents", headers=self.principal, files={"file": ("policy.txt", b"ATTENDANCE POLICY\n\nStudents must maintain 75% attendance to sit exams.", "text/plain")}, data={"title": "Policy", "classification": "internal", "category": "policy"})
        self.assertEqual(upload.status_code, 200, upload.text)
        search = self.client.post("/v1/documents/search", headers=self.student, json={"question": "attendance to sit exams"}).json()
        self.assertEqual(search["sources"][0]["title"], "Policy")
        self.assertEqual(self.client.post("/v1/documents", headers=self.student, files={"file": ("x.txt", b"text", "text/plain")}).status_code, 403)
        listed = self.client.get("/v1/documents", headers=self.student).json()["documents"]
        self.assertTrue(listed)
        self.assertEqual(self.client.delete(f"/v1/documents/{upload.json()['document_id']}", headers=self.principal).json()["deleted"], True)

    def test_intelligence_routes_without_provider(self):
        put = self.client.put("/v1/intelligence/profile", headers=self.principal, json={"name": "Route College", "location": "Mysuru", "official_domains": ["route.example"], "exclusions": ["Chennai"]})
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.json()["profile"]["official_domains"], ["route.example"])
        self.assertEqual(self.client.get("/v1/intelligence/profile", headers=self.principal).json()["configured"], False)
        self.assertEqual(self.client.post("/v1/intelligence/investigate", headers=self.principal, json={}).status_code, 503)
        self.assertEqual(self.client.put("/v1/intelligence/profile", headers=self.student, json={"name": "Route College"}).status_code, 403)
        self.assertEqual(self.client.get("/v1/intelligence/mentions", headers=self.principal).json()["mentions"], [])

    def test_readiness_reports_platform_state_and_openapi_lists_routes(self):
        ready = self.client.get("/v1/health/ready").json()
        self.assertTrue(ready["platform"]["enabled"])
        self.assertEqual(ready["platform"]["object_store"], "memory")
        paths = set(app.openapi()["paths"])
        for path in ("/v1/ingestion/uploads", "/v1/agent/commands", "/v1/documents/search", "/v1/intelligence/investigate", "/v1/reports/{report_id}/download"):
            self.assertIn(path, paths)

    def test_upload_size_limit_applies_only_to_upload_routes(self):
        from app.config.settings import AppSettings

        settings = AppSettings.from_env()
        too_big = b"x" * (settings.max_request_bytes + 10)
        self.assertEqual(self.client.post("/v1/chat", headers=self.principal, content=too_big, headers_extra={} if False else None).status_code if False else self.client.post("/v1/chat", headers={**self.principal, "Content-Type": "application/json"}, content=too_big).status_code, 413)
        big_csv = b"Student Name,USN\n" + b"\n".join(b"Name %d,ID%d" % (i, i) for i in range(60_000))
        self.assertGreater(len(big_csv), settings.max_request_bytes)
        response = self.client.post("/v1/ingestion/uploads", headers=self.principal, files={"file": ("big.csv", big_csv, "text/csv")}, data={"process": "false"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_json_document_routes_keep_the_tight_body_limit(self):
        from app.config.settings import AppSettings

        settings = AppSettings.from_env()
        too_big = b"x" * (settings.max_request_bytes + 10)
        for method, path in (("POST", "/v1/documents/search"), ("DELETE", "/v1/documents/some-document"), ("POST", "/v1/ingestion/uploads/extra")):
            response = self.client.request(method, path, headers={**self.principal, "Content-Type": "application/json"}, content=too_big)
            self.assertEqual(response.status_code, 413, f"{method} {path}: {response.text[:200]}")
        big_document = b"Attendance policy paragraph. " * (settings.max_request_bytes // 20)
        self.assertGreater(len(big_document), settings.max_request_bytes)
        upload = self.client.post("/v1/documents", headers=self.principal, files={"file": ("big.txt", big_document, "text/plain")}, data={"title": "Big", "category": "policy"})
        self.assertEqual(upload.status_code, 200, upload.text[:200])
        self.client.delete(f"/v1/documents/{upload.json()['document_id']}", headers=self.principal)

    def test_blocking_store_and_object_store_work_runs_off_the_event_loop(self):
        platform = app.state.runtime.platform
        seen: dict[str, tuple[bool, asyncio.AbstractEventLoop | None]] = {}

        def record(name):
            def _mark():
                try:
                    loop = asyncio.get_running_loop()
                    on_loop = True
                except RuntimeError:
                    loop = None
                    on_loop = False
                seen[name] = (on_loop, loop)
            return _mark

        # Captures the loop that handles each ingestion request, on the request loop itself.
        from app.api.routes import ingestion as ingestion_routes

        real_require = ingestion_routes.require_principal
        request_loops: list[asyncio.AbstractEventLoop] = []

        def require_probe(request, *args, **kwargs):
            request_loops.append(asyncio.get_running_loop())
            return real_require(request, *args, **kwargs)

        # Positive control: the notifications inbox is called synchronously by its
        # route, so the probe must observe a running loop there.
        def inbox_probe(service, principal, institution_id, *, limit=50, unread_only=False):
            record("inbox")()
            return []

        def ping_probe():
            record("ping")()
            return True

        # The services are slots dataclasses, so their probes are patched on the class and take ``self``.
        def commit_probe(service, institution_id, job_id, *, committed_by):
            record("commit")()
            return {"job_id": job_id, "institution_id": institution_id, "status": "imported"}

        async def apply_mapping_probe(service, institution_id, job_id, **kwargs):
            record("apply_mapping")()
            return {"job_id": job_id, "institution_id": institution_id, "status": "imported"}

        def fetch_probe(service, principal, institution_id, report_id):
            record("report_fetch")()
            return {"object_key": f"{institution_id}/reports/{report_id}.csv", "format": "csv"}, b"a,b\n"

        with mock.patch.object(platform.store, "ping", ping_probe), \
                mock.patch.object(type(platform.ingestion), "commit", commit_probe), mock.patch.object(type(platform.ingestion), "apply_mapping", apply_mapping_probe), \
                mock.patch.object(type(platform.reports), "fetch", fetch_probe), mock.patch.object(type(platform.notifications), "inbox", inbox_probe), mock.patch.object(ingestion_routes, "require_principal", require_probe):
            self.assertEqual(self.client.get("/v1/notifications", headers=self.principal).status_code, 200)
            self.assertEqual(self.client.get("/v1/health/ready").json()["status"], "ready")
            self.assertEqual(self.client.post(f"/v1/ingestion/jobs/{self.job_id}/commit", headers=self.principal).status_code, 200)
            self.assertEqual(self.client.post(f"/v1/ingestion/jobs/{self.job_id}/mapping", headers=self.principal, json={"mapping": {}, "entity": "student"}).status_code, 200)
            self.assertEqual(self.client.get("/v1/reports/r1/download", headers=self.principal).status_code, 200)
        # Thread identities are not compared: the test client runs each request's
        # event loop in a fresh thread, so identities get reused. A worker thread
        # never has a running loop, which is the property that matters.
        self.assertTrue(seen["inbox"][0], "the control probe did not observe the event loop")
        for name in ("ping", "commit", "report_fetch"):
            on_loop, _ident = seen[name]
            self.assertFalse(on_loop, f"{name} ran on the event loop")
        # apply_mapping runs to completion on its own loop inside a pool thread, never on the request loop.
        on_loop, loop = seen["apply_mapping"]
        self.assertTrue(on_loop, "apply_mapping did not run under its own loop")
        self.assertIsNot(loop, request_loops[-1], "apply_mapping ran on the request loop")


if __name__ == "__main__":
    unittest.main()
