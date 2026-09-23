"""Full-duplex voice: speech events, barge-in, protected agent turns, expiry."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import importlib.util
import unittest

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.agents.contracts import AgentResponse
    from app.conversation.contracts import Reply
    from app.main import app
    from app.voice.tts import SynthesizedSpeech

HEADERS = {
    "Authorization": "Bearer dev-token",
    "X-Demo-Principal": "voice-duplex-test",
    "X-Demo-Role": "student",
    "X-Demo-College": "college_a",
}


class _FakeDialogue:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.turns = []

    async def respond(self, turn, *, on_progress=None):
        self.turns.append(turn)
        text = turn.text
        try:
            if text == "slow chat":
                await asyncio.sleep(5)
            if text == "agent action":
                await on_progress("agent", "", _Lang())
                await asyncio.sleep(0.3)
                self.finished.append(text)
                return Reply("task", AgentResponse(turn.request_id, "complete", "Report created. Download: /v1/reports/r1/download"), "en-IN", "Report created.", side_effects=True)
            if text == "search":
                await on_progress("web_search", "One moment, let me check the internet.", _Lang())
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        answer = "Namaste! How can I help? I can also search the internet for you."
        return Reply("small_talk", AgentResponse(turn.request_id, "complete", answer, intent="small_talk.greeting"), "en-IN", answer)


class _Lang:
    code = "en-IN"
    hinglish = False


class _FakeTTS:
    provider = "polly"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def supports(self, language: str) -> bool:
        return language in {"en-IN", "hi-IN"}

    async def synthesize(self, text, *, language, cacheable=False):
        self.spoken.append(text)
        return SynthesizedSpeech(audio=b"ID3" + text.encode(), language=language, voice="Kajal")

    def describe(self):
        return {"provider": "polly", "voice": "Kajal", "engine": "neural", "languages": ["en-IN", "hi-IN"]}

    def close(self):
        return None


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class FullDuplexVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self.runtime = app.state.runtime
        self.saved = (self.runtime.dialogue, self.runtime.tts, self.runtime.settings)
        self.dialogue = _FakeDialogue()
        self.tts = _FakeTTS()
        self.runtime.dialogue = self.dialogue
        self.runtime.tts = self.tts

    def tearDown(self) -> None:
        self.runtime.dialogue, self.runtime.tts, self.runtime.settings = self.saved

    def _open(self, features=("thinking", "speech", "interrupt")):
        response = self.client.post("/v1/voice/sessions", headers=HEADERS, json={"college_id": "college_a"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["languages"], ["en-IN", "hi-IN", "kn-IN"])
        self.assertIn("tts", payload)
        socket = self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream")
        websocket = socket.__enter__()
        self.addCleanup(socket.__exit__, None, None, None)
        websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"], "features": list(features)})
        ready = websocket.receive_json()
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["features"], sorted(features))
        self.assertEqual(ready["tts"]["voice"], "Kajal")
        return websocket

    def test_a_turn_streams_thinking_answer_then_spoken_sentences(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "client_message_id": "t1", "text": "hello", "language": "en-IN", "history": [{"role": "user", "text": "hi"}]})
        self.assertEqual(websocket.receive_json(), {"type": "thinking", "client_message_id": "t1"})
        answer = websocket.receive_json()
        self.assertEqual((answer["type"], answer["answer"]["route"], answer["answer"]["spoken"]), ("answer", "small_talk", True))
        speech = [websocket.receive_json()]
        while not speech[-1]["final"]:
            speech.append(websocket.receive_json())
        self.assertTrue(all(item["type"] == "speech" and item["client_message_id"] == "t1" for item in speech))
        self.assertEqual([item["seq"] for item in speech], list(range(len(speech))))
        self.assertEqual(base64.b64decode(speech[0]["audio_base64"])[:3], b"ID3")
        self.assertEqual(speech[0]["voice"], "Kajal")
        self.assertEqual(self.dialogue.turns[0].history[0].text, "hi")
        self.assertEqual(self.dialogue.turns[0].channel, "voice")

    def test_a_web_search_speaks_a_filler_first(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "client_message_id": "w1", "text": "search"})
        self.assertEqual(websocket.receive_json()["type"], "thinking")
        filler = websocket.receive_json()
        self.assertEqual((filler["type"], filler["filler"], filler["final"]), ("speech", True, False))
        self.assertIn("internet", filler["text"])
        self.assertEqual(websocket.receive_json()["type"], "answer")

    def test_talking_over_a_reply_cancels_it(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "client_message_id": "slow", "text": "slow chat"})
        self.assertEqual(websocket.receive_json()["type"], "thinking")
        websocket.send_json({"type": "interrupt", "client_message_id": "slow"})
        self.assertEqual(websocket.receive_json(), {"type": "cancelled", "client_message_id": "slow", "reason": "interrupted"})
        websocket.send_json({"type": "utterance", "client_message_id": "next", "text": "hello"})
        self.assertEqual(websocket.receive_json()["client_message_id"], "next")
        self.assertEqual(websocket.receive_json()["type"], "answer")
        self.assertEqual(self.dialogue.cancelled, ["slow chat"])

    def test_a_new_utterance_supersedes_the_pending_one(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "client_message_id": "a", "text": "slow chat"})
        self.assertEqual(websocket.receive_json()["type"], "thinking")
        websocket.send_json({"type": "utterance", "client_message_id": "b", "text": "hello"})
        self.assertEqual(websocket.receive_json(), {"type": "cancelled", "client_message_id": "a", "reason": "superseded"})
        self.assertEqual(websocket.receive_json(), {"type": "thinking", "client_message_id": "b"})
        self.assertEqual(websocket.receive_json()["answer"]["request_id"][:6], "voice-")

    def test_an_agent_action_is_never_cut_short(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "client_message_id": "act", "text": "agent action"})
        self.assertEqual(websocket.receive_json()["type"], "thinking")
        websocket.send_json({"type": "interrupt"})
        answer = websocket.receive_json()
        self.assertEqual((answer["type"], answer["client_message_id"], answer["answer"]["spoken"]), ("answer", "act", False))
        websocket.send_json({"type": "ping"})
        self.assertEqual(websocket.receive_json()["type"], "pong", "a silenced reply sends no speech")
        self.assertEqual(self.dialogue.finished, ["agent action"])
        self.assertEqual(self.dialogue.cancelled, [])

    def test_an_oversized_utterance_is_refused_and_the_socket_stays_open(self) -> None:
        websocket = self._open()
        websocket.send_json({"type": "utterance", "text": "x" * 4001})
        self.assertEqual(websocket.receive_json()["code"], "invalid_event")
        websocket.send_json({"type": "ping"})
        self.assertEqual(websocket.receive_json()["type"], "pong")

    def test_a_failing_turn_reports_an_error_and_keeps_the_conversation(self) -> None:
        class _Broken(_FakeDialogue):
            async def respond(self, turn, *, on_progress=None):
                if turn.text == "boom":
                    raise RuntimeError("database gone")
                return await super().respond(turn, on_progress=on_progress)

        self.runtime.dialogue = _Broken()
        websocket = self._open(features=())
        with self.assertLogs("guru.voice", "ERROR"):
            websocket.send_json({"type": "utterance", "client_message_id": "x", "text": "boom"})
            error = websocket.receive_json()
        self.assertEqual((error["type"], error["code"], error["client_message_id"]), ("error", "turn_failed", "x"))
        self.assertNotIn("database", error["message"])
        websocket.send_json({"type": "utterance", "client_message_id": "y", "text": "hello"})
        self.assertEqual(websocket.receive_json()["type"], "answer", "without features only the answer is sent")

    def test_an_idle_conversation_expires(self) -> None:
        self.runtime.settings = dataclasses.replace(self.runtime.settings, voice_idle_timeout_seconds=0.3)
        websocket = self._open()
        self.assertEqual(websocket.receive_json(), {"type": "expired", "reason": "idle"})
        with self.assertRaises(WebSocketDisconnect) as closed:
            websocket.receive_json()
        self.assertEqual(closed.exception.code, 4001)

    def test_opening_voice_again_ends_the_older_conversation(self) -> None:
        first = self._open()
        self._open()
        self._open()
        first.send_json({"type": "utterance", "text": "hello"})
        self.assertEqual(first.receive_json(), {"type": "expired", "reason": "closed"})


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class ConversationRouteTests(unittest.TestCase):
    """The real dialogue behind the routes (deterministic model, no web search)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_voice_small_talk_is_spoken_by_the_browser_when_polly_is_off(self) -> None:
        payload = self.client.post("/v1/voice/sessions", headers=HEADERS, json={"college_id": "college_a"}).json()
        with self.client.websocket_connect(f"/v1/voice/sessions/{payload['session_id']}/stream") as websocket:
            websocket.send_json({"type": "auth", "ticket": payload["transport_ticket"], "features": ["speech"]})
            self.assertEqual(websocket.receive_json()["tts"]["provider"], "browser")
            websocket.send_json({"type": "utterance", "client_message_id": "n", "text": "नमस्ते"})
            answer = websocket.receive_json()["answer"]
            self.assertEqual((answer["route"], answer["language"]), ("small_talk", "hi-IN"))
            speech = websocket.receive_json()
            self.assertEqual((speech["type"], speech["audio_base64"], speech["language"]), ("speech", None, "hi-IN"))
            self.assertIn("नमस्ते", speech["text"])

    def test_text_chat_answers_small_talk_and_keeps_institutional_answers(self) -> None:
        hello = self.client.post("/v1/agent/commands", headers={**HEADERS, "X-Demo-Role": "faculty"}, json={"command": "hello", "history": [{"role": "user", "text": "hi"}], "language": "en-IN"})
        self.assertEqual(hello.status_code, 200, hello.text)
        self.assertEqual((hello.json()["route"], hello.json()["status"]), ("small_talk", "complete"))
        chat = self.client.post("/v1/chat", headers=HEADERS, json={"prompt": "thank you", "conversational": True, "institution_scope": {"college_id": "college_a"}})
        self.assertEqual((chat.json()["route"], chat.json()["status"]), ("small_talk", "complete"))
        legacy = self.client.post("/v1/chat", headers=HEADERS, json={"prompt": "What is the current attendance summary?", "institution_scope": {"college_id": "college_a"}})
        self.assertNotIn("route", legacy.json(), "without conversational the chat contract is unchanged")
        outside = self.client.post("/v1/chat", headers=HEADERS, json={"prompt": "hello", "conversational": True, "institution_scope": {"college_id": "college_b"}})
        self.assertEqual(outside.status_code, 403)


if __name__ == "__main__":
    unittest.main()
