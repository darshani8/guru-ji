"""Document search, internet intelligence, and ingestion status tools (optional services)."""

from __future__ import annotations

from typing import Any

from ..domain.principals import Capability
from ..gateway.spec import PlatformToolSpec, RiskLevel, ToolCallContext, ToolOutput, param
from .common import output
from .context import PlatformServices


def build_knowledge_tools(services: PlatformServices) -> tuple[PlatformToolSpec, ...]:
    tools: list[PlatformToolSpec] = []
    documents = services.documents
    intelligence = services.intelligence
    ingestion = services.ingestion

    if documents is not None:
        async def search_documents(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
            result = await documents.answer(context.principal, context.institution_id, args["question"], top_k=args.get("top_k", 5), category=args.get("category"))
            provenance_items = [{"source_id": f"{context.institution_id}:document:{item['document_id']}", "source_type": "internal_api", "retrieved_at": result["searched_at"], "rows_used": 1, "complete": True, "title": item["title"], "locator": item["locator"]} for item in result["sources"]]
            out = output(context.institution_id, "documents", result, result["answer"], rows_used=len(result["sources"]), warnings=result.get("warnings", []))
            out.provenance = provenance_items or out.provenance
            return out

        async def list_documents(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
            rows = documents.list(context.principal, context.institution_id, limit=args.get("limit", 50), category=args.get("category"))
            return output(context.institution_id, "documents", {"count": len(rows), "documents": rows}, f"{len(rows)} document(s) indexed.", rows_used=len(rows))

        tools.extend([
            PlatformToolSpec(
                name="search_documents", group="documents", description="Answer a question from the institution's uploaded policies, circulars, and documents, citing document and page.",
                required_capability=Capability.DOCUMENTS_READ, handler=search_documents, returns="{answer, sources[]}",
                parameters=(param("question", "string", "Question to answer from documents", required=True, max_length=500), param("top_k", "integer", "Passages to consider", minimum=1, maximum=10, default=5), param("category", "string", "Document category filter", max_length=40)),
                examples=("What is the attendance policy?",),
            ),
            PlatformToolSpec(name="list_documents", group="documents", description="List indexed institutional documents.", required_capability=Capability.DOCUMENTS_READ, handler=list_documents, returns="{count, documents[]}", parameters=(param("limit", "integer", "Maximum rows", minimum=1, maximum=200, default=50), param("category", "string", "Category filter", max_length=40))),
        ])

    if intelligence is not None:
        async def internet_investigate(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
            result = await intelligence.investigate(context.principal, context.institution_id, question=args.get("question"), window_days=args.get("window_days", 7), topics=args.get("topics"), max_results=args.get("max_results", 10))
            provenance_items = [
                {"source_id": item["url"], "source_type": "public_web", "retrieved_at": item["retrieved_at"], "rows_used": 1, "complete": True, "title": item["title"], "published_at": item.get("published_at"), "source_kind": item["source_type"], "match": item["match_level"]}
                for item in result["findings"]
            ]
            out = output(context.institution_id, "internet", result, result["summary"], rows_used=len(result["findings"]), warnings=result.get("warnings", []))
            out.provenance = provenance_items or out.provenance
            return out

        async def get_intelligence_digest(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
            result = intelligence.digest(context.principal, context.institution_id, days=args.get("days", 1))
            return output(context.institution_id, "internet", result, result["summary"], rows_used=result.get("total_items", 0), records_returned=0)

        tools.extend([
            PlatformToolSpec(
                name="internet_investigate", group="internet", description="Search public web sources (news, official sites, education portals, public social pages) for information about this institution in a time window; returns source-backed findings.",
                required_capability=Capability.INTELLIGENCE_READ, handler=internet_investigate, returns="{summary, findings[], excluded}",
                parameters=(param("question", "string", "What to look for", max_length=300), param("window_days", "integer", "Days back to search", minimum=1, maximum=365, default=7), param("topics", "array", "Topics such as admission, placement, event, news", max_length=10), param("max_results", "integer", "Maximum findings", minimum=1, maximum=30, default=10)),
                examples=("What happened about our college on the internet this week?",),
            ),
            PlatformToolSpec(name="get_intelligence_digest", group="internet", description="Daily digest of new internet mentions from continuous monitoring.", required_capability=Capability.INTELLIGENCE_READ, handler=get_intelligence_digest, returns="{summary, counts}", parameters=(param("days", "integer", "Days back", minimum=1, maximum=30, default=1),)),
        ])

    if ingestion is not None:
        async def get_ingestion_status(context: ToolCallContext, args: dict[str, Any]) -> ToolOutput:
            if not context.principal.has_capability(Capability.DATA_INGEST):
                raise PermissionError("data:ingest capability is required")
            jobs = services.store.list_jobs(context.institution_id, limit=args.get("limit", 10))
            items = [{"job_id": job["job_id"], "status": job["status"], "stage": job["stage"], "entity": job.get("entity"), "rows": job.get("row_count"), "created_at": job["created_at"], "import": (job.get("report") or {}).get("import")} for job in jobs]
            counts = services.store.entity_counts(context.institution_id)
            return output(context.institution_id, "ingestion", {"jobs": items, "record_counts": counts}, f"{len(items)} recent ingestion job(s); {sum(counts.values())} canonical records on file.", rows_used=len(items), records_returned=0)

        tools.append(PlatformToolSpec(name="get_ingestion_status", group="ingestion", description="Recent data import jobs and canonical record counts.", required_capability=Capability.DATA_INGEST, handler=get_ingestion_status, risk=RiskLevel.READ, returns="{jobs[], record_counts}", parameters=(param("limit", "integer", "Maximum jobs", minimum=1, maximum=50, default=10),)))
    return tuple(tools)


__all__ = ["build_knowledge_tools"]
