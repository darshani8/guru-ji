import importlib.util
import unittest

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


if __name__ == "__main__":
    unittest.main()
