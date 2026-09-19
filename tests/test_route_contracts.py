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

    def test_chat_stream_returns_answer_and_done_events(self):
        with self.client.stream(
            "POST",
            "/v1/chat/stream",
            headers=self.headers,
            json={"prompt": "What is the current attendance summary?", "institution_scope": {"college_id": "college_a"}},
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = response.read().decode("utf-8")
        self.assertIn("event: answer", body)
        self.assertIn('"status":"complete"', body)
        self.assertIn("event: done", body)

        query_response = self.client.post(
            "/v1/chat?stream=true",
            headers=self.headers,
            json={"prompt": "Give me the institutional overview", "institution_scope": {"college_id": "college_a"}},
        )
        self.assertEqual(query_response.status_code, 200)
        self.assertIn("text/event-stream", query_response.headers["content-type"])
        self.assertIn("event: done", query_response.text)

        accept_response = self.client.post(
            "/v1/chat",
            headers={**self.headers, "Accept": "text/event-stream"},
            json={"prompt": "Are the approved sources healthy?", "institution_scope": {"college_id": "college_a"}},
        )
        self.assertEqual(accept_response.status_code, 200)
        self.assertIn("event: answer", accept_response.text)


if __name__ == "__main__":
    unittest.main()
