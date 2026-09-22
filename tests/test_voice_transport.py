import importlib.util
import unittest

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class VoiceTransportRouteTests(unittest.TestCase):
    headers = {
        "Authorization": "Bearer dev-token",
        "X-Demo-Principal": "voice-route-test",
        "X-Demo-Role": "student",
        "X-Demo-College": "college_a",
    }

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def create_session(self, **body):
        response = self.client.post("/v1/voice/sessions", headers=self.headers, json=body or None)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_session_returns_one_use_browser_transport_ticket(self):
        payload = self.create_session(college_id="college_a")
        self.assertEqual(payload["transport"], "browser_web_speech_ws")
        self.assertTrue(payload["transport_ticket"])
        self.assertTrue(payload["websocket_url"].endswith(f"/v1/voice/sessions/{payload['session_id']}/stream"))
        self.assertEqual(payload["server_receives"], "final_transcripts_only")

    def test_websocket_routes_final_transcript_through_voice_chat(self):
        payload = self.create_session(college_id="college_a")
        with self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream") as websocket:
            websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"]})
            self.assertEqual(websocket.receive_json()["type"], "ready")
            websocket.send_json({
                "type": "utterance",
                "client_message_id": "voice-test-1",
                "text": "What is the current attendance summary?",
            })
            message = websocket.receive_json()
            self.assertEqual(message["type"], "answer")
            self.assertEqual(message["client_message_id"], "voice-test-1")
            self.assertEqual(message["answer"]["status"], "complete")
            self.assertEqual(message["answer"]["generation_mode"], "deterministic")
            self.assertTrue(message["answer"]["request_id"].startswith("voice-"))

    def test_ticket_cannot_be_replayed_after_transport_disconnect(self):
        payload = self.create_session(college_id="college_a")
        with self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream") as websocket:
            websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"]})
            self.assertEqual(websocket.receive_json()["type"], "ready")
        with self.assertRaises(WebSocketDisconnect) as context:
            with self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream") as replay:
                replay.send_json({"type": "auth", "ticket": payload["transport_ticket"]})
                replay.receive_json()
        self.assertEqual(context.exception.code, 4401)

    def test_client_cannot_create_session_outside_granted_college(self):
        response = self.client.post(
            "/v1/voice/sessions",
            headers=self.headers,
            json={"college_id": "college_b"},
        )
        self.assertEqual(response.status_code, 403)

    def test_binary_audio_is_rejected_by_transcript_transport(self):
        payload = self.create_session(college_id="college_a")
        with self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream") as websocket:
            websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"]})
            self.assertEqual(websocket.receive_json()["type"], "ready")
            websocket.send_bytes(b"raw-audio-is-not-accepted")
            error = websocket.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertEqual(error["code"], "binary_audio_not_supported")
            with self.assertRaises(WebSocketDisconnect) as context:
                websocket.receive_json()
        self.assertEqual(context.exception.code, 1003)

    def test_loopback_origin_is_allowed_on_a_local_dev_port(self):
        payload = self.create_session(college_id="college_a")
        with self.client.websocket_connect(
            f"/v1/voice/sessions/{payload['session_id']}/stream",
            headers={"origin": "http://127.0.0.1:8878"},
        ) as websocket:
            websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"]})
            self.assertEqual(websocket.receive_json()["type"], "ready")

    def test_unconfigured_external_origin_is_rejected(self):
        payload = self.create_session(college_id="college_a")
        with self.assertRaises(WebSocketDisconnect) as context:
            with self.client.websocket_connect(
                f"/v1/voice/sessions/{payload['session_id']}/stream",
                headers={"origin": "https://evil.example"},
            ) as websocket:
                websocket.receive_json()
        self.assertEqual(context.exception.code, 1008)


if __name__ == "__main__":
    unittest.main()
