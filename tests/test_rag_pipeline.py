import asyncio
import json
import unittest
from pathlib import Path

import httpx

from app.documents.evals import EvalCase, RagEvaluator, generation_metrics, retrieval_metrics
from app.documents.rerank import HttpReranker, LexicalReranker
from app.documents.vector_store import InstitutionVectorStore
from app.domain.principals import PrincipalType
from platform_fixtures import PlatformFixture, principal

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "evals" / "rag"


class _FailingReranker:
    provider_name = "broken"

    async def rerank(self, query, passages, *, top_n):
        raise RuntimeError("down")


class _ReverseReranker:
    provider_name = "reverse"

    async def rerank(self, query, passages, *, top_n):
        from app.documents.rerank import RerankResult

        return [RerankResult(index, float(index)) for index in reversed(range(len(passages)))][:top_n]


class _Judge:
    provider_id = "judge"

    def __init__(self, reply):
        self.reply = reply

    async def complete(self, prompt, *, max_tokens=800):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class RerankerTests(unittest.TestCase):
    def test_lexical_reranker_prefers_passages_covering_the_query(self):
        reranker = LexicalReranker()
        passages = ["Library books may be borrowed for 14 days.", "Hostel gates close at 9:30 pm.", "A late fee of Rs. 100 per day applies after the fee due date."]
        ranked = asyncio.run(reranker.rerank("late fee after due date", passages, top_n=2))
        self.assertEqual([item.index for item in ranked][0], 2)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(asyncio.run(reranker.rerank("anything", [], top_n=3)), [])
        unrelated = asyncio.run(reranker.rerank("quantum chromodynamics", passages, top_n=3))
        self.assertTrue(all(item.score == 0 for item in unrelated))

    def test_http_reranker_reads_cohere_and_tei_responses(self):
        seen = {}

        def cohere(request):
            seen.update(json.loads(request.content))
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]})

        reranker = HttpReranker(base_url="https://rerank.example/v2/", api_key="k", model_id="m", transport=httpx.MockTransport(cohere))
        ranked = asyncio.run(reranker.rerank("q", ["a", "b"], top_n=5))
        self.assertEqual([(item.index, item.score) for item in ranked], [(1, 0.9), (0, 0.2)])
        self.assertEqual((seen["model"], seen["top_n"], seen["documents"], seen["auth"]), ("m", 2, ["a", "b"], "Bearer k"))

        tei = HttpReranker(base_url="http://tei", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{"index": 0, "score": 0.1}, {"index": 1, "score": 0.7}])))
        self.assertEqual(asyncio.run(tei.rerank("q", ["a", "b"], top_n=1))[0].index, 1)
        bad = HttpReranker(base_url="http://tei", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [{"index": 9, "score": 1}]})))
        with self.assertRaises(ValueError):
            asyncio.run(bad.rerank("q", ["a"], top_n=1))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = PlatformFixture(seed=False)
        self.pri = principal(PrincipalType.PRINCIPAL)
        self.ids = {}
        for path in sorted((SAMPLE / "documents").iterdir()):
            result = await self.fx.documents.ingest(self.pri, "college_a", file_name=path.name, content=path.read_bytes(), content_type="text/plain", title=path.stem)
            self.ids[path.name] = result["document_id"]

    async def test_retrieve_top_k_then_rerank_to_top_n(self):
        self.assertIsInstance(self.fx.documents.vector_store, InstitutionVectorStore)
        result = await self.fx.documents.retrieve(self.pri, "college_a", "late fee after the due date", top_k=2, retrieve_k=4)
        self.assertLessEqual(len(result["candidates"]), 4)
        self.assertEqual(len(result["passages"]), 2)
        self.assertEqual(result["reranked_by"], "lexical-bm25")
        self.assertEqual(result["passages"][0]["document_id"], self.ids["fee_policy.txt"])
        self.assertIsNotNone(result["passages"][0]["rerank_score"])
        answer = await self.fx.documents.answer(self.pri, "college_a", "late fee after the due date", top_k=2, retrieve_k=4)
        self.assertEqual(answer["pipeline"]["reranked_by"], "lexical-bm25")
        self.assertEqual(answer["pipeline"]["used"], 2)
        self.assertNotIn("trace", answer)

    async def test_reranker_order_wins_and_failures_fall_back_to_retrieval_order(self):
        self.fx.documents.reranker = _ReverseReranker()
        result = await self.fx.documents.retrieve(self.pri, "college_a", "attendance fee library hostel", top_k=3, retrieve_k=3)
        self.assertEqual([item["retrieval_rank"] for item in result["passages"]], [3, 2, 1])
        self.fx.documents.reranker = _FailingReranker()
        result = await self.fx.documents.retrieve(self.pri, "college_a", "attendance fee library hostel", top_k=3, retrieve_k=3)
        self.assertEqual([item["retrieval_rank"] for item in result["passages"]], [1, 2, 3])
        self.assertEqual(result["warnings"][0]["code"], "reranker_unavailable")
        self.assertEqual(result["reranked_by"], "none")

    async def test_min_rerank_score_drops_unrelated_passages(self):
        self.fx.documents.min_rerank_score = 0.05
        answer = await self.fx.documents.answer(self.pri, "college_a", "Which companies visited for campus placements?")
        self.assertEqual(answer["sources"], [])
        self.assertTrue((await self.fx.documents.answer(self.pri, "college_a", "When do hostel gates close?"))["sources"])

    async def test_evaluator_scores_the_sample_set(self):
        cases = json.loads((SAMPLE / "cases.json").read_text())["cases"]
        for case in cases:
            case["expected_document_ids"] = [self.ids[name] for name in case.pop("expected_files", [])]
        self.fx.documents.min_rerank_score = 0.05
        report = await RagEvaluator(self.fx.documents).evaluate(self.pri, "college_a", cases, top_k=3)
        summary = report["summary"]
        self.assertEqual(summary["pass_rate"], 1.0, [case for case in report["cases"] if not case["passed"]])
        self.assertEqual(summary["reranked"]["hit"], 1.0)
        self.assertGreaterEqual(summary["reranked"]["precision"], summary["retrieval"]["precision"])
        self.assertEqual(summary["generation"]["citation_validity"], 1.0)
        self.assertEqual(summary["abstention_accuracy"], 1.0)

        self.fx.documents.min_rerank_score = None
        unanswerable = await RagEvaluator(self.fx.documents).evaluate(self.pri, "college_a", [{"question": "Which companies visited for campus placements?", "answerable": False}])
        self.assertFalse(unanswerable["cases"][0]["passed"])
        with self.assertRaises(ValueError):
            await RagEvaluator(self.fx.documents).evaluate(self.pri, "college_a", [])
        with self.assertRaises(PermissionError):
            await RagEvaluator(self.fx.documents).evaluate(principal(PrincipalType.PRINCIPAL, college_id="college_b"), "college_a", cases)

    async def test_judge_scores_are_clamped_and_failures_ignored(self):
        case = [{"question": "When do hostel gates close on weekends?", "reference_answer": "10:30 pm"}]
        judged = await RagEvaluator(self.fx.documents, judge=_Judge('Scores: {"faithfulness": 1.4, "relevance": 0.8, "correctness": "x"}')).evaluate(self.pri, "college_a", case)
        self.assertEqual(judged["cases"][0]["judge"], {"faithfulness": 1.0, "relevance": 0.8})
        self.assertEqual(judged["summary"]["judge"]["relevance"], 0.8)
        failed = await RagEvaluator(self.fx.documents, judge=_Judge(RuntimeError("down"))).evaluate(self.pri, "college_a", case)
        self.assertIsNone(failed["cases"][0]["judge"])


class MetricTests(unittest.TestCase):
    def test_retrieval_metrics(self):
        case = EvalCase("q", expected_document_ids=("doc-a", "doc-b"))
        ranked = [{"document_id": "doc-x", "text": ""}, {"document_id": "doc-a", "text": ""}, {"document_id": "doc-b", "text": ""}]
        metrics = retrieval_metrics(case, ranked)
        self.assertEqual((metrics["hit"], metrics["recall"], metrics["mrr"], metrics["precision"]), (1.0, 1.0, 0.5, 0.6667))
        self.assertLess(metrics["ndcg"], 1.0)
        self.assertIsNone(retrieval_metrics(EvalCase("q"), ranked))
        text_case = EvalCase("q", expected_text="75%", expected_pages=(2,))
        self.assertEqual(retrieval_metrics(text_case, [{"document_id": "d", "page_number": 1, "text": "75%"}])["hit"], 0.0)
        self.assertEqual(retrieval_metrics(text_case, [{"document_id": "d", "page_number": 2, "text": "needs 75% attendance"}])["hit"], 1.0)

    def test_generation_metrics(self):
        passages = [{"document_id": "doc-1a", "text": "Students must maintain a minimum of 75% attendance to write the examination."}]
        case = EvalCase("What minimum attendance is required?", reference_answer="A minimum of 75% attendance.", must_contain=("75%",))
        grounded = generation_metrics(case, "Students must maintain a minimum of 75% attendance [doc-1a:page 1].", passages, abstained=False)
        self.assertEqual((grounded["faithfulness"], grounded["citation_validity"], grounded["citation_coverage"], grounded["keyword_coverage"]), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(grounded["answer_recall"], 1.0)
        invented = generation_metrics(case, "Hostel residents receive free parking permits every semester [doc-ff:page 2].", passages, abstained=False)
        self.assertEqual((invented["faithfulness"], invented["citation_validity"], invented["keyword_coverage"]), (0.0, 0.0, 0.0))
        self.assertIsNone(generation_metrics(case, "", passages, abstained=True)["faithfulness"])


if __name__ == "__main__":
    unittest.main()
