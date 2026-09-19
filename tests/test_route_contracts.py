import importlib.util
import unittest

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed in this Codespace")
class RouteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.headers = {
            "Authorization": "Bearer dev-token",
            "X-Demo-Principal": "route-test",
            "X-Demo-Role": "student",
            "X-Demo-College": "college_a",
        }

    def test_liveness_route(self):
        response = self.client.get("/v1/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_chat_route_returns_citation_and_warning(self):
        response = self.client.post(
            "/v1/chat", headers=self.headers,
            json={"prompt": "What is the current attendance summary?", "institution_scope": {"college_id": "college_a"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "complete")
        self.assertTrue(response.json()["citations"])
        self.assertTrue(response.json()["warnings"])

    def test_student_cannot_view_source_metadata(self):
        response = self.client.get("/v1/sources", headers=self.headers)
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
