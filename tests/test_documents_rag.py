import unittest

from app.documents.chunking import chunk_texts
from app.documents.embeddings import HashingEmbeddingProvider, cosine
from app.domain.principals import PrincipalType
from app.ingestion.models import ParsedText
from platform_fixtures import PlatformFixture, principal

POLICY = b"ATTENDANCE POLICY\n\nStudents must maintain a minimum of 75% attendance in every course to be eligible for end semester examinations. Students between 65% and 75% may be condoned on medical grounds.\n\nFEE POLICY\n\nTuition fees are payable in two instalments. A late fee of Rs. 100 per day applies after the due date.\n\nLIBRARY RULES\n\nBooks may be borrowed for 14 days.\n"


class _CitingModel:
    provider_id = "cite-model"
    model_id = "x"

    def __init__(self, cite: bool):
        self.cite = cite

    async def complete(self, prompt, *, max_tokens=800):
        if not self.cite:
            return "Students need 75% attendance."
        import re

        marker = re.search(r"\[(doc-[0-9a-f]+:[^\]]+)\]", prompt).group(0)
        return f"Students need 75% attendance to sit exams {marker}."


class ChunkingAndEmbeddingTests(unittest.TestCase):
    def test_chunks_keep_headings_with_their_paragraph_and_overlap(self):
        chunks = chunk_texts([ParsedText("body", POLICY.decode(), page=1)], chunk_chars=140, overlap_chars=20)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(chunks[0].text.startswith("ATTENDANCE POLICY Students"))
        self.assertTrue(all(chunk.page_number == 1 for chunk in chunks))
        self.assertEqual(len(chunk_texts([ParsedText("b", "word " * 3000)])), 20)
        with self.assertRaises(ValueError):
            chunk_texts([], chunk_chars=10, overlap_chars=10)

    def test_hashing_embeddings_are_deterministic_and_semantic_enough(self):
        provider = HashingEmbeddingProvider(dimensions=256)
        import asyncio

        a, b, c = asyncio.run(provider.embed(["attendance policy for examinations", "minimum attendance to write exams", "library book borrowing"]))
        self.assertEqual(a, asyncio.run(provider.embed(["attendance policy for examinations"]))[0])
        self.assertGreater(cosine(a, b), cosine(a, c))
        self.assertEqual(cosine([], []), 0.0)


class DocumentRagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture(seed=False)
        self.pri = principal(PrincipalType.PRINCIPAL)
        self.stu = principal(PrincipalType.STUDENT)

    async def test_ingest_search_and_answer_with_sources(self):
        result = await self.fx.documents.ingest(self.pri, "college_a", file_name="policies.txt", content=POLICY, content_type="text/plain", title="Academic Policies", category="policy")
        self.assertGreaterEqual(result["chunks"], 1)
        hits = await self.fx.documents.search(self.stu, "college_a", "minimum attendance for exams")
        self.assertEqual(hits[0]["title"], "Academic Policies")
        self.assertIn("75%", hits[0]["text"])
        answer = await self.fx.documents.answer(self.stu, "college_a", "What is the late fee?")
        self.assertIn("late fee", answer["answer"].lower())
        self.assertEqual(answer["sources"][0]["locator"], "page 1")
        self.assertEqual(answer["generation_mode"], "deterministic")
        empty = await self.fx.documents.answer(self.stu, "college_a", "quantum chromodynamics", category="nothing")
        self.assertEqual(empty["sources"], [])

    async def test_classification_controls_visibility(self):
        await self.fx.documents.ingest(self.pri, "college_a", file_name="board.txt", content=b"Board resolution: salary revision approved for faculty in October.", content_type="text/plain", title="Board minutes", classification="confidential")
        self.assertEqual(self.fx.documents.list(self.stu, "college_a"), [])
        self.assertEqual(len(self.fx.documents.list(self.pri, "college_a")), 1)
        self.assertEqual((await self.fx.documents.answer(self.stu, "college_a", "salary revision"))["sources"], [])
        self.assertTrue((await self.fx.documents.answer(self.pri, "college_a", "salary revision"))["sources"])
        with self.assertRaises(PermissionError):
            await self.fx.documents.ingest(self.stu, "college_a", file_name="x.txt", content=b"text", content_type="text/plain")
        with self.assertRaises(ValueError):
            await self.fx.documents.ingest(self.pri, "college_a", file_name="x.txt", content=b"text", content_type="text/plain", classification="secret")

    async def test_model_answers_must_cite_retrieved_documents(self):
        await self.fx.documents.ingest(self.pri, "college_a", file_name="policies.txt", content=POLICY, content_type="text/plain", title="Academic Policies")
        self.fx.documents.model = _CitingModel(cite=False)
        rejected = await self.fx.documents.answer(self.stu, "college_a", "attendance requirement")
        self.assertEqual(rejected["generation_mode"], "deterministic")
        self.assertIn("model_output_rejected", [w["code"] for w in rejected["warnings"]])
        self.fx.documents.model = _CitingModel(cite=True)
        accepted = await self.fx.documents.answer(self.stu, "college_a", "attendance requirement")
        self.assertEqual(accepted["generation_mode"], "cite-model")
        self.assertIn("[doc-", accepted["answer"])

    async def test_agent_answers_document_questions_with_citations(self):
        await self.fx.documents.ingest(self.pri, "college_a", file_name="policies.txt", content=POLICY, content_type="text/plain", title="Academic Policies")
        from app.agents.contracts import AgentCommand
        from app.domain.principals import InstitutionScope

        response = await self.fx.agent.handle(AgentCommand("r", self.stu, InstitutionScope("college_a"), "What is the attendance policy?"))
        self.assertEqual(response.status, "complete")
        self.assertEqual(response.sources[0]["title"], "Academic Policies")


if __name__ == "__main__":
    unittest.main()
