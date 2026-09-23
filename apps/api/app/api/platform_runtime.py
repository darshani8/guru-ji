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
from ..internet_intelligence.fetch import PublicPageFetcher, crawler_user_agent
from ..internet_intelligence.map.connectors.apis import CourtRecordsConnector, WikidataConnector, YouTubeConnector
from ..internet_intelligence.map.connectors.archive import WaybackConnector
from ..internet_intelligence.map.connectors.base import ConnectorRegistry
from ..internet_intelligence.map.connectors.common import ApiClient
from ..internet_intelligence.map.connectors.feeds import FeedConnector
from ..internet_intelligence.map.connectors.hubs import DirectoryConnector, LinkHubConnector
from ..internet_intelligence.map.connectors.infrastructure import CertificateConnector, DnsConnector, RdapConnector
from ..internet_intelligence.map.connectors.places import GooglePlayConnector, OpenStreetMapConnector
from ..internet_intelligence.map.connectors.search import SearchConnector, SpamProbeConnector
from ..internet_intelligence.map.connectors.web import LeadPageConnector, OfficialSiteConnector, RecheckConnector
from ..internet_intelligence.map.engine import EngineConfig, MapEngine
from ..internet_intelligence.map.incidents import IncidentDesk
from ..internet_intelligence.map.ownership import OwnerClaimsConnector
from ..internet_intelligence.map.service import MapService
from ..internet_intelligence.map.store import MapStore
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
    worker_store: InstitutionDataStore | None = None
    intelligence_map: MapService | None = None

    def close(self, *, worker_timeout: float = 30.0) -> None:
        stop = getattr(self.jobs, "stop", None)
        if callable(stop):
            # Let the job in flight finish (bounded) before its store goes away.
            stop(timeout=worker_timeout)
        if self.worker_store is not None and self.worker_store is not self.store:
            self.worker_store.close()
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


def _job_queue(settings: AppSettings, store: InstitutionDataStore, worker_store: InstitutionDataStore | None) -> JobQueue:
    # The in-process worker gets its own store (connection and lock) whenever
    # the database allows a second connection, so a long import transaction
    # never delays request-path store calls; an in-memory SQLite database is
    # shared because a second connection would be a different database.
    if settings.job_queue == "thread":
        return ThreadJobQueue(store, worker_store=worker_store, stale_seconds=settings.job_stale_seconds)
    if settings.job_queue == "sqs":
        return SqsJobQueue(store, settings.sqs_queue_url or "", region=settings.s3_region, stale_seconds=settings.job_stale_seconds)
    return InlineJobQueue(store, stale_seconds=settings.job_stale_seconds)


@dataclass(slots=True)
class _Services:
    ingestion: IngestionService
    data: InstitutionDataService
    reports: ReportService
    email: EmailService
    notifications: NotificationService
    documents: DocumentRagService
    intelligence: InternetIntelligenceService | None
    monitor: ContinuousMonitor | None
    registry: PlatformToolRegistry
    gateway: ToolGateway


def _services(settings: AppSettings, store: InstitutionDataStore, intelligence_store: IntelligenceStore, *, objects: ObjectStore, parsers: ParserRegistry, mapping: MappingEngine, control_store: ControlStore, pdp: PolicyDecisionPoint, tracer: TraceRecorder, model: TextModel | None, provider: Any | None) -> _Services:
    """The service graph bound to one store; built twice when the worker has its own connection."""

    ingestion = IngestionService(store=store, objects=objects, parsers=parsers, mapping=mapping, max_upload_bytes=settings.max_upload_bytes, max_rows=settings.ingestion_max_rows, auto_commit=settings.ingestion_auto_commit, restart_after_seconds=max(settings.job_stale_seconds, 900))
    data = InstitutionDataService(store)
    reports = ReportService(store, objects)
    email = EmailService(store, objects, _email_sender(settings), allowed_domains=settings.email_allowed_domains)
    notifications = NotificationService(store, control_store)
    documents = DocumentRagService(store, objects, parsers, embeddings=_embeddings(settings), model=model, max_document_bytes=settings.max_upload_bytes)
    intelligence: InternetIntelligenceService | None = None
    monitor: ContinuousMonitor | None = None
    if provider is not None:
        fetcher = PublicPageFetcher(timeout_seconds=settings.web_extract_timeout_seconds, max_response_bytes=settings.web_extract_max_bytes, user_agent=crawler_user_agent(settings.intelligence_crawler_contact)) if settings.intelligence_fetch_pages else None
        intelligence = InternetIntelligenceService(
            intelligence_store, provider, fetcher=fetcher, institution_store=store, model=model, max_queries=settings.intelligence_max_queries, results_per_query=settings.intelligence_results_per_query,
            investigations_per_day=settings.intelligence_investigations_per_day,
        )

        def alert_sink(institution_id: str, recipients: Any, title: str, body: str) -> None:
            notifications.system_notify(institution_id, recipient_ids=list(recipients), title=title, body=body, reference_type="monitoring")

        monitor = ContinuousMonitor(intelligence, intelligence_store, alert_sink=alert_sink)
    services = PlatformServices(store=store, data=data, reports=reports, email=email, notifications=notifications, documents=documents, intelligence=intelligence, ingestion=ingestion)
    registry = build_platform_registry(services)
    gateway = ToolGateway(registry, store, control_store, pdp=pdp, tracer=tracer, approval_ttl_seconds=settings.approval_ttl_seconds)
    return _Services(ingestion, data, reports, email, notifications, documents, intelligence, monitor, registry, gateway)


_SHARED_ONLY_URLS = frozenset({":memory:", "sqlite:///:memory:", ""})


def _map_service(
    settings: AppSettings, backend: Any, intelligence_store: IntelligenceStore, provider: Any | None, notifications: NotificationService | None = None, email: EmailService | None = None,
) -> MapService:
    fetcher = PublicPageFetcher(timeout_seconds=settings.web_extract_timeout_seconds, max_response_bytes=settings.web_extract_max_bytes, user_agent=crawler_user_agent(settings.intelligence_crawler_contact)) if settings.intelligence_fetch_pages else None
    store = MapStore(backend=backend, suppression_key=_suppression_key(settings))

    def recipients(institution_id: str) -> tuple[str, ...]:
        profile = intelligence_store.get_profile(institution_id)
        return profile.alert_recipients if profile else ()

    def contacts(institution_id: str) -> tuple[str, ...]:
        profile = intelligence_store.get_profile(institution_id)
        return profile.security_contacts if profile else ()

    def notify(institution_id: str, recipient_ids: Any, title: str, body: str, incident_id: str) -> list[str]:
        # What was created is returned: the desk marks an incident notified only once something was delivered.
        if notifications is None:
            return []
        return notifications.system_notify(institution_id, recipient_ids=list(recipient_ids), title=title, body=body, reference_type="internet_map", reference_id=incident_id or None)

    desk = IncidentDesk(
        store, recipients=recipients, notify=notify, contacts=contacts, email=email.system_send if email is not None else None, authority_contacts=settings.intelligence_authority_contact_map(),
    )
    engine = MapEngine(
        store, map_connectors(settings), EngineConfig(sources_per_tick=settings.intelligence_sources_per_tick, budgets=settings.intelligence_budget_caps(), tenant_share=settings.intelligence_tenant_share),
        fetcher=fetcher, search=provider, profile_loader=intelligence_store.get_profile, incidents=desk,
    )
    return MapService(store, fetcher=fetcher, engine=engine, seed_groups=settings.intelligence_seed_group_map(), desk=desk, approvers=settings.intelligence_entity_approver_map())


def _suppression_key(settings: AppSettings) -> bytes:
    return (settings.intelligence_suppression_key or "guru-ji-development-only").encode("utf-8")


def map_connectors(settings: AppSettings, *, transport: Any | None = None) -> ConnectorRegistry:
    """The connectors the map engine may use.

    The public-page connectors (and the owner check, which reads only the
    institution's own domains) are always on; every other one stays off
    until GURU_INTELLIGENCE_CONNECTORS names it (and its key is configured).
    """

    on = set(settings.intelligence_connectors)
    client = ApiClient(user_agent=crawler_user_agent(settings.intelligence_crawler_contact), timeout_seconds=settings.web_extract_timeout_seconds, transport=transport)
    return ConnectorRegistry([
        OfficialSiteConnector(), LeadPageConnector(), RecheckConnector(),
        SearchConnector(active="search" in on), SpamProbeConnector(active="spam_probe" in on), FeedConnector(active="feed" in on),
        YouTubeConnector(api_key=settings.intelligence_youtube_api_key or "", active="youtube" in on, client=client),
        WikidataConnector(active="wikidata" in on, client=client),
        CourtRecordsConnector(api_token=settings.intelligence_indiankanoon_token or "", active="court_records" in on, client=client),
        CertificateConnector(active="certificates" in on, client=client), RdapConnector(active="rdap" in on, client=client), DnsConnector(active="dns" in on, client=client),
        WaybackConnector(active="wayback" in on, client=client), LinkHubConnector(active="link_hub" in on), DirectoryConnector(active="directory" in on),
        OpenStreetMapConnector(active="openstreetmap" in on, client=client), GooglePlayConnector(active="google_play" in on),
        # Reads only the institution's own domains; the DNS TXT check uses DNS over HTTPS only when the dns connector is allowed.
        OwnerClaimsConnector(key=_suppression_key(settings), dns=client if "dns" in on else None),
    ])


def build_platform(settings: AppSettings, *, control_store: ControlStore, pdp: PolicyDecisionPoint, tracer: TraceRecorder, model: TextModel | None, planner_model: TextModel | None = None, institution_store: InstitutionDataStore | None = None, objects: ObjectStore | None = None, search_provider: Any | None = None, start_workers: bool = False) -> PlatformRuntime:
    """Assemble the platform.

    ``start_workers`` is set only by processes meant to run background jobs
    (the API with the thread queue, the worker script): it starts the polling
    thread and the stale-job sweep. Scripts and one-off commands leave it off
    so they never claim jobs they will not finish.
    """

    database_url = settings.resolved_institution_database_url()
    store = institution_store or InstitutionDataStore(database_url)
    intelligence_store = IntelligenceStore(backend=store.backend)
    objects = objects or _object_store(settings)
    parsers = ParserRegistry(ocr_engine=_ocr_engine(settings), max_bytes=settings.max_upload_bytes)
    mapping = MappingEngine(threshold=settings.mapping_confidence_threshold, model=model)
    provider = search_provider
    if provider is None and settings.intelligence_search_provider == "tavily":
        provider = TavilyIntelligenceSearchProvider(api_key=settings.web_search_api_key or "", endpoint=settings.web_search_endpoint, timeout_seconds=settings.web_search_timeout_seconds)
    build = dict(objects=objects, parsers=parsers, mapping=mapping, control_store=control_store, pdp=pdp, tracer=tracer, model=model, provider=provider)
    request = _services(settings, store, intelligence_store, **build)
    intelligence_map = _map_service(settings, store.backend, intelligence_store, provider, request.notifications, request.email) if settings.intelligence_map_enabled else None
    # The in-process worker works on its own connection when the database can
    # open one, so its transactions never hold the request path's lock.
    worker_store: InstitutionDataStore | None = None
    if settings.job_queue == "thread" and start_workers and institution_store is None and database_url not in _SHARED_ONLY_URLS:
        worker_store = InstitutionDataStore(database_url)
    jobs = _job_queue(settings, store, worker_store)
    planner_model = planner_model or model
    model_planner = ModelPlanner(planner_model) if (settings.agent_planner == "model" and planner_model is not None) else None
    agent = MasterAgent(request.gateway, request.registry, request.data, store, control_store, planner=DeterministicPlanner(), model_planner=model_planner, model=model, model_max_tokens=settings.model_max_tokens, tracer=tracer, background=jobs)
    if worker_store is not None:
        worker_intelligence_store = IntelligenceStore(backend=worker_store.backend)
        worker = _services(settings, worker_store, worker_intelligence_store, **build)
        worker_map = _map_service(settings, worker_store.backend, worker_intelligence_store, provider, worker.notifications, worker.email) if settings.intelligence_map_enabled else None
        worker_agent = MasterAgent(worker.gateway, worker.registry, worker.data, worker_store, control_store, planner=DeterministicPlanner(), model_planner=model_planner, model=model, model_max_tokens=settings.model_max_tokens, tracer=tracer, background=jobs)
        register_handlers(jobs, ingestion=worker.ingestion, agent=worker_agent, monitor=worker.monitor, notifications=worker.notifications, map_engine=worker_map.engine if worker_map else None, map_desk=worker_map.desk if worker_map else None)
    else:
        register_handlers(jobs, ingestion=request.ingestion, agent=agent, monitor=request.monitor, notifications=request.notifications, map_engine=intelligence_map.engine if intelligence_map else None, map_desk=intelligence_map.desk if intelligence_map else None)
    if start_workers:
        # The thread queue only wakes on enqueue, so start it at boot rather than
        # on the first upload, then hand back jobs whose worker stopped reporting.
        start = getattr(jobs, "start", None)
        if callable(start):
            start()
        jobs.recover_stale()
    return PlatformRuntime(
        store=store, intelligence_store=intelligence_store, objects=objects, parsers=parsers, ingestion=request.ingestion, data=request.data, reports=request.reports, email=request.email,
        notifications=request.notifications, documents=request.documents, registry=request.registry, gateway=request.gateway, agent=agent, jobs=jobs, intelligence=request.intelligence, monitor=request.monitor,
        sheets=GoogleSheetsCsvConnector(max_bytes=settings.max_upload_bytes), worker_store=worker_store, intelligence_map=intelligence_map,
    )


__all__ = ["PlatformRuntime", "build_platform", "map_connectors"]
