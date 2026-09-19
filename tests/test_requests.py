"""Tests for bounded text and voice request contracts."""

import unittest

from app.domain.principals import InstitutionScope
from app.domain.requests import (
    MAX_PROMPT_CHARS,
    MAX_SOURCE_IDS,
    ChatRequest,
    InteractionChannel,
)


class ChatRequestTests(unittest.TestCase):
    def make_request(self, **overrides: object) -> ChatRequest:
        values: dict[str, object] = {
            "request_id": "request-1",
            "principal_id": "faculty-1",
            "prompt": "Explain the attendance policy.",
            "institution_scope": InstitutionScope("college-a"),
            "source_ids": ("policy-1", "policy-2"),
        }
        values.update(overrides)
        return ChatRequest(**values)  # type: ignore[arg-type]

    def test_request_normalizes_text_and_channel(self) -> None:
        request = self.make_request(
            request_id=" request-1 ",
            principal_id=" faculty-1 ",
            prompt=" Explain the attendance policy. ",
            conversation_id=" conversation-1 ",
            channel="voice",
            source_ids=["policy-1", "policy-2"],
        )

        self.assertEqual(request.request_id, "request-1")
        self.assertEqual(request.principal_id, "faculty-1")
        self.assertEqual(request.prompt, "Explain the attendance policy.")
        self.assertEqual(request.conversation_id, "conversation-1")
        self.assertEqual(request.channel, InteractionChannel.VOICE)
        self.assertEqual(request.source_ids, ("policy-1", "policy-2"))

    def test_prompt_and_source_limits_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "prompt exceeds"):
            self.make_request(prompt="x" * (MAX_PROMPT_CHARS + 1))

        with self.assertRaisesRegex(ValueError, "source_ids exceeds"):
            self.make_request(source_ids=tuple(f"source-{index}" for index in range(MAX_SOURCE_IDS + 1)))

        with self.assertRaisesRegex(ValueError, "unique"):
            self.make_request(source_ids=("policy-1", "policy-1"))

    def test_required_text_fields_cannot_be_blank(self) -> None:
        for field_name in ("request_id", "principal_id", "prompt"):
            with self.subTest(field_name=field_name), self.assertRaises(ValueError):
                self.make_request(**{field_name: " "})

        with self.assertRaises(ValueError):
            self.make_request(conversation_id=" ")

    def test_source_ids_cannot_contain_blank_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_id"):
            self.make_request(source_ids=("policy-1", " "))


if __name__ == "__main__":
    unittest.main()
