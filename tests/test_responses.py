"""Tests for cited answer and refusal response contracts."""

import unittest

from app.domain.responses import AnswerResponse, Citation, ResponseStatus


class ResponseTests(unittest.TestCase):
    def test_complete_response_serializes_citations(self) -> None:
        citation = Citation(
            source_id="catalog-1",
            title="Academic Catalogue",
            locator="section:attendance",
        )
        response = AnswerResponse(
            request_id="request-1",
            status="complete",
            answer="Attendance is recorded each working day.",
            citations=(citation,),
        )

        self.assertEqual(response.status, ResponseStatus.COMPLETE)
        self.assertEqual(
            response.as_dict(),
            {
                "request_id": "request-1",
                "status": "complete",
                "answer": "Attendance is recorded each working day.",
                "citations": [
                    {
                        "source_id": "catalog-1",
                        "title": "Academic Catalogue",
                        "locator": "section:attendance",
                    }
                ],
                "refusal_reason": None,
            },
        )

    def test_refused_response_requires_and_serializes_reason(self) -> None:
        response = AnswerResponse(
            request_id="request-2",
            status=ResponseStatus.REFUSED,
            answer="",
            refusal_reason="The request is outside the approved institution scope.",
        )

        self.assertEqual(response.status, ResponseStatus.REFUSED)
        self.assertEqual(
            response.as_dict()["refusal_reason"],
            "The request is outside the approved institution scope.",
        )

        with self.assertRaisesRegex(ValueError, "refusal_reason is required"):
            AnswerResponse("request-3", ResponseStatus.REFUSED, "")

    def test_non_refused_response_requires_answer_and_no_refusal_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "answer must not be blank"):
            AnswerResponse("request-4", ResponseStatus.COMPLETE, " ")

        with self.assertRaisesRegex(ValueError, "only valid for refused"):
            AnswerResponse(
                "request-5",
                ResponseStatus.PARTIAL,
                "Some evidence was found.",
                refusal_reason="unexpected",
            )

    def test_citation_fields_cannot_be_blank(self) -> None:
        with self.assertRaisesRegex(ValueError, "title"):
            Citation("source-1", " ", "page:1")

        with self.assertRaisesRegex(ValueError, "locator"):
            Citation("source-1", "Policy", " ")


if __name__ == "__main__":
    unittest.main()
