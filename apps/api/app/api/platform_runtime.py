"""Construction of the institutional data platform runtime from settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..actions.email import EmailSender, EmailService, OutboxEmailSender, SesEmailSender, SmtpEmailSender
from ..actions.notifications import NotificationService
from ..actions.reports import ReportService
from ..agents.master import MasterAgent
from ..agents.planner import DeterministicPlanner, ModelPlanner
from ..config.settings import AppSettings
from ..data_access.service import InstitutionDataService
from ..documents.embeddings import EmbeddingProvider, HashingEmbeddingProvider, OllamaEmbeddingProvider, OpenAICompatibleEmbeddingProvider
from ..documents.rag import DocumentRagService
from ..gateway.gateway import ToolGateway
from ..gateway.registry import PlatformToolRegistry
from ..ingestion.connectors.sheets import GoogleSheetsCsvConnector
from ..ingestion.parsers.ocr import DisabledOcrEngine, OcrEngine, TesseractCliOcrEngine, TextractOcrEngine
from ..ingestion.registry import ParserRegistry
from ..ingestion.service import IngestionService
from ..institution_data.store import InstitutionDataStore
from ..internet_intelligence.fetch import PublicPageFetcher
from ..internet_intelligence.monitoring import ContinuousMonitor
from ..internet_intelligence.search import TavilyIntelligenceSearchProvider
from ..internet_intelligence.service import InternetIntelligenceService
from ..internet_intelligence.store import IntelligenceStore
from ..normalization.mapping import MappingEngine
from ..observability.tracing import TraceRecorder
from ..persistence.database import InMemoryControlStore, PostgresControlStore, SqliteControlStore
from ..platform_tools.build import build_platform_registry
from ..platform_tools.context import PlatformServices
from ..policy.pdp import PolicyDecisionPoint
from ..providers.model_base import TextModel
from ..storage.object_store import InMemoryObjectStore, LocalFileObjectStore, ObjectStore, S3ObjectStore
from ..workers.handlers import register_handlers
from ..workers.queue import InlineJobQueue, JobQueue, SqsJobQueue, ThreadJobQueue

ControlStore = InMemoryControlStore | PostgresControlStore | SqliteControlStore


@dataclass(slots=True)
class PlatformRuntime:
    store: InstitutionDataStore
    intelligence_store: IntelligenceStore
    objects: ObjectStore
    parsers: ParserRegistry
    ingestion: IngestionService
    data: InstitutionDataService
    reports: ReportService
    email: EmailService
    notifications: NotificationService
    documents: DocumentRagService
    registry: PlatformToolRegistry
    gateway: ToolGateway
    agent: MasterAgent
    jobs: JobQueue
    intelligence: InternetIntelligenceService | None = None
    monitor: ContinuousMonitor | None = None
    sheets: GoogleSheetsCsvConnector | None = None

    def close(self) -> None:
        stop = getattr(self.jobs, "stop", None)
        if callable(stop):
            stop()
        self.store.close()
        if self.intelligence_store.backend is not self.store.backend:
            self.intelligence_store.close()


def _object_store(settings: AppSettings) -> ObjectStore:
    if settings.object_store_backend == "memory":
        return InMemoryObjectStore()
    if settings.object_store_backend == "s3":
        return S3ObjectStore(settings.s3_bucket or "", prefix=settings.s3_prefix, region=settings.s3_region)
    return LocalFileObjectStore(settings.object_store_path)


def _ocr_engine(settings: AppSettings) -> OcrEngine:
    if settings.ocr_engine == "tesseract":
        return TesseractCliOcrEngine(languages=settings.ocr_languages)
    if settings.ocr_engine == "textract":
        return TextractOcrEngine(region=settings.s3_region)
    return DisabledOcrEngine()


def _email_sender(settings: AppSettings) -> EmailSender:
    if settings.email_provider == "smtp":
        return SmtpEmailSender(host=settings.smtp_host or "", port=settings.smtp_port, sender=settings.email_sender or "", username=settings.smtp_username, password=settings.smtp_password, use_tls=settings.smtp_use_tls)
    if settings.email_provider == "ses":
        return SesEmailSender(settings.email_sender or "", region=settings.s3_region)
    return OutboxEmailSender()


def _embeddings(settings: AppSettings) -> EmbeddingProvider:
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingProvider(base_url=settings.embedding_base_url or "", model_id=settings.embedding_model_id or "nomic-embed-text")
    if settings.embedding_provider == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(base_url=settings.embedding_base_url or "", api_key=settings.embedding_api_key or "", model_id=settings.embedding_model_id or "text-embedding-3-small")
    return HashingEmbeddingProvider()


def _job_queue(settings: AppSettings, store: InstitutionDataStore) -> JobQueue:
    # The worker shares the request path's store (one connection guarded by a
    # process-wide lock). Request handlers call the store off the event loop,
    # so an import holds the lock for its (atomic) transaction without ever
    # blocking the loop: readiness and job listings wait for that transaction
    # instead of stalling the API. Production runs the SQS worker in its own
    # process with its own connection, where nothing waits.
    if settings.job_queue == "thread":
        return ThreadJobQueue(store)
    if settings.job_queue == "sqs":
        return SqsJobQueue(store, settings.sqs_queue_url or "", region=settings.s3_region)
    return InlineJobQueue(store)


def build_platform(settings: AppSettings, *, control_store: ControlStore, pdp: PolicyDecisionPoint, tracer: TraceRecorder, model: TextModel | None, institution_store: InstitutionDataStore | None = None, objects: ObjectStore | None = None, search_provider: Any | None = None) -> PlatformRuntime:
    store = institution_store or InstitutionDataStore(settings.resolved_institution_database_url())
    intelligence_store = IntelligenceStore(backend=store.backend)
    objects = objects or _object_store(settings)
    parsers = ParserRegistry(ocr_engine=_ocr_engine(settings), max_bytes=settings.max_upload_bytes)
    mapping = MappingEngine(threshold=settings.mapping_confidence_threshold, model=model)
    ingestion = IngestionService(store=store, objects=objects, parsers=parsers, mapping=mapping, max_upload_bytes=settings.max_upload_bytes, max_rows=settings.ingestion_max_rows, auto_commit=settings.ingestion_auto_commit, restart_after_seconds=settings.job_stale_seconds)
    data = InstitutionDataService(store)
    reports = ReportService(store, objects)
    email = EmailService(store, objects, _email_sender(settings), allowed_domains=settings.email_allowed_domains)
    notifications = NotificationService(store, control_store)
    documents = DocumentRagService(store, objects, parsers, embeddings=_embeddings(settings), model=model, max_document_bytes=settings.max_upload_bytes)
    intelligence: InternetIntelligenceService | None = None
    monitor: ContinuousMonitor | None = None
    provider = search_provider
    if provider is None and settings.intelligence_search_provider == "tavily":
        provider = TavilyIntelligenceSearchProvider(api_key=settings.web_search_api_key or "", endpoint=settings.web_search_endpoint, timeout_seconds=settings.web_search_timeout_seconds)
    if provider is not None:
        fetcher = PublicPageFetcher(timeout_seconds=settings.web_extract_timeout_seconds, max_response_bytes=settings.web_extract_max_bytes) if settings.intelligence_fetch_pages else None
        intelligence = InternetIntelligenceService(intelligence_store, provider, fetcher=fetcher, institution_store=store, model=model, max_queries=settings.intelligence_max_queries, results_per_query=settings.intelligence_results_per_query)

        def alert_sink(institution_id: str, recipients: Any, title: str, body: str) -> None:
            notifications.system_notify(institution_id, recipient_ids=list(recipients), title=title, body=body, reference_type="monitoring")

        monitor = ContinuousMonitor(intelligence, intelligence_store, alert_sink=alert_sink)
    services = PlatformServices(store=store, data=data, reports=reports, email=email, notifications=notifications, documents=documents, intelligence=intelligence, ingestion=ingestion)
    registry = build_platform_registry(services)
    gateway = ToolGateway(registry, store, control_store, pdp=pdp, tracer=tracer, approval_ttl_seconds=settings.approval_ttl_seconds)
    model_planner = ModelPlanner(model) if (settings.agent_planner == "model" and model is not None) else None
    jobs = _job_queue(settings, store)
    agent = MasterAgent(gateway, registry, data, store, control_store, planner=DeterministicPlanner(), model_planner=model_planner, model=model, model_max_tokens=settings.model_max_tokens, tracer=tracer, background=jobs)
    register_handlers(jobs, ingestion=ingestion, agent=agent, monitor=monitor, notifications=notifications)
    # The thread queue only wakes on enqueue, so start it at boot rather than on
    # the first upload, then hand back jobs a previous process left ``running``.
    start = getattr(jobs, "start", None)
    if callable(start):
        start()
    jobs.recover_stale(older_than_seconds=settings.job_stale_seconds)
    return PlatformRuntime(
        store=store, intelligence_store=intelligence_store, objects=objects, parsers=parsers, ingestion=ingestion, data=data, reports=reports, email=email,
        notifications=notifications, documents=documents, registry=registry, gateway=gateway, agent=agent, jobs=jobs, intelligence=intelligence, monitor=monitor,
        sheets=GoogleSheetsCsvConnector(max_bytes=settings.max_upload_bytes),
    )


__all__ = ["PlatformRuntime", "build_platform"]
