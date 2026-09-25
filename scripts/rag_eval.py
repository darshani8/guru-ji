"""Run the document RAG pipeline over an eval set and print the scores.

Ingests every file in --documents into an in-memory store with the configured
embedding provider and reranker, runs the labelled questions in --cases, and
prints the summary (or the full report with --json). Cases may name files in
``expected_files``; they become the document ids assigned at ingestion.
Exits non-zero when the pass rate is below --min-pass-rate, so CI can gate on it.

    make rag-eval
    PYTHONPATH=apps/api python scripts/rag_eval.py --reranker none   # compare without reranking
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))


async def run(args: argparse.Namespace) -> int:
    from app.api.platform_runtime import _embeddings, _reranker
    from app.auth.roles import capabilities_for_role
    from app.config.settings import AppSettings
    from app.documents.evals import RagEvaluator
    from app.documents.rag import DocumentRagService
    from app.domain.principals import InstitutionScope, Principal, PrincipalType
    from app.ingestion.registry import ParserRegistry
    from app.institution_data.store import InstitutionDataStore
    from app.storage.object_store import InMemoryObjectStore

    settings = AppSettings.from_env()
    reranker = _reranker(_with_reranker(settings, args.reranker) if args.reranker else settings)
    service = DocumentRagService(InstitutionDataStore(":memory:"), InMemoryObjectStore(), ParserRegistry(), embeddings=_embeddings(settings), reranker=reranker, retrieve_k=args.retrieve_k or settings.rag_retrieve_k, min_rerank_score=settings.rag_min_rerank_score if args.min_rerank_score is None else args.min_rerank_score)
    institution = "eval_college"
    service.store.upsert_institution(institution, "Evaluation College")
    principal = Principal("rag-eval", PrincipalType.PRINCIPAL, capabilities_for_role(PrincipalType.PRINCIPAL), (InstitutionScope(institution),))

    documents: dict[str, str] = {}
    for path in sorted(Path(args.documents).iterdir()):
        if not path.is_file():
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result = await service.ingest(principal, institution, file_name=path.name, content=path.read_bytes(), content_type=content_type, title=path.stem.replace("_", " ").title())
        documents[path.name] = result["document_id"]
    if not documents:
        raise SystemExit(f"no documents found in {args.documents}")

    raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = raw["cases"] if isinstance(raw, dict) else raw
    for case in cases:
        files = case.pop("expected_files", None) or []
        missing = [name for name in files if name not in documents]
        if missing:
            raise SystemExit(f"case {case.get('case_id') or case['question']!r} names unknown files: {', '.join(missing)}")
        case["expected_document_ids"] = list(case.get("expected_document_ids") or []) + [documents[name] for name in files]

    report = await RagEvaluator(service).evaluate(principal, institution, cases, top_k=args.top_k, retrieve_k=args.retrieve_k)
    report["documents"] = documents
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_summary(report, service)
    return 0 if report["summary"]["pass_rate"] >= args.min_pass_rate else 1


def _with_reranker(settings, provider: str):
    from dataclasses import replace

    return replace(settings, rerank_provider=provider)


def _print_summary(report: dict, service) -> None:
    summary = report["summary"]
    print(f"embeddings={service.embeddings.provider_name} reranker={service.reranker.provider_name if service.reranker else 'none'} top_k={report['settings']['top_k']} retrieve_k={report['settings']['retrieve_k']}")
    print(f"cases={summary['cases']} passed={summary['passed']} pass_rate={summary['pass_rate']:.2f}")
    for stage in ("retrieval", "reranked"):
        if summary[stage]:
            print(f"{stage:>10}: " + "  ".join(f"{key}={value:.3f}" for key, value in summary[stage].items() if value is not None))
    print("generation: " + "  ".join(f"{key}={value:.3f}" for key, value in summary["generation"].items() if value is not None))
    if summary["abstention_accuracy"] is not None:
        print(f"abstention_accuracy={summary['abstention_accuracy']:.2f}")
    for case in report["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        rank = case["reranked"]["mrr"] if case["reranked"] else None
        print(f"  [{mark}] {case['case_id']}: mrr={rank} faithfulness={case['generation']['faithfulness']} answer_recall={case['generation']['answer_recall']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--documents", default=str(ROOT / "evals" / "rag" / "documents"))
    parser.add_argument("--cases", default=str(ROOT / "evals" / "rag" / "cases.json"))
    parser.add_argument("--top-k", type=int, default=3, help="passages kept after reranking (top-N)")
    parser.add_argument("--retrieve-k", type=int, default=None, help="candidates pulled from the vector store (top-K)")
    parser.add_argument("--reranker", choices=("none", "lexical", "http"), default=None, help="override SAFFRON_RERANK_PROVIDER")
    parser.add_argument("--min-rerank-score", type=float, default=None, help="override SAFFRON_RAG_MIN_RERANK_SCORE")
    parser.add_argument("--min-pass-rate", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
