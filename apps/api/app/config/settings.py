"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..config.institution_connectors import (
    InstitutionConnectorDefinition,
    parse_institution_connectors,
)
from ..providers.anthropic import DEFAULT_MODEL_ID as ANTHROPIC_DEFAULT_MODEL_ID
from ..providers.anthropic import EFFORT_LEVELS as ANTHROPIC_EFFORT_LEVELS
from ..providers.anthropic import OPEN_TASK_MODEL_ID as OPEN_TASK_DEFAULT_MODEL_ID
from ..web_research.domain_allowlist import DEFAULT_ALLOWED_DOMAINS
from .legacy_env import adopt_legacy_names


def _origins_from_env(name: str, *extra: str | None) -> tuple[str, ...]:
    """Collect scheme://host[:port] origins for the browser's connect policy.

    Anything carrying a path, credentials or a non-HTTP(S) scheme is dropped
    rather than rejected, so a malformed entry cannot widen the policy or take
    the service down at start-up.
    """

    candidates = [item.strip() for item in (os.getenv(name) or "").split(",")]
    candidates.extend(item for item in extra if item)
    origins: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlparse(candidate.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.username or parsed.password:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _default_control_database_url() -> str:
    """The local development database; the file made before the rename keeps being used."""

    if not os.path.exists("./data/agentic_saffron.db") and os.path.exists("./data/guru_ji.db"):
        return "sqlite:///./data/guru_ji.db"
    return "sqlite:///./data/agentic_saffron.db"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_EMAIL_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _group_lists(raw: str, name: str, what: str) -> dict[str, tuple[str, ...]]:
    """A JSON object of sweep group label to a list of non-blank strings (approvers, contacts)."""

    if not raw:
        return {}
    import json

    message = f"{name} must be a JSON object of sweep group label to a list of {what}"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(message) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value) for key, value in parsed.items()
    ):
        raise ValueError(message)
    return {key.strip(): tuple(item.strip() for item in value) for key, value in parsed.items()}


# Connectors the internet map may use beyond the always-on public-page ones
# (official_site, lead_page, recheck); each stays off until named in
# SAFFRON_INTELLIGENCE_CONNECTORS.
OPTIONAL_CONNECTORS: tuple[str, ...] = (
    "search", "spam_probe", "feed", "youtube", "wikidata", "court_records", "certificates", "rdap", "dns", "wayback", "link_hub", "directory", "openstreetmap", "google_play",
    "news_feed", "lookalike_domains", "google_play_search",
)


@dataclass(frozen=True, slots=True)
class AppSettings:
    app_name: str = "agentic-saffron-api"
    version: str = "0.1.0"
    environment: str = "development"
    allowed_origins: tuple[str, ...] = ("http://localhost:5173",)
    dev_bearer_token: str = field(default="dev-token", repr=False)
    control_database_url: str | None = field(default_factory=_default_control_database_url, repr=False)
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    # Origins the browser must reach to complete a login: the issuer's discovery
    # document and the provider's token endpoint, which for Cognito sits on a
    # different host than the issuer. Listed explicitly so the page's
    # Content-Security-Policy can stay closed to everything else.
    oidc_browser_origins: tuple[str, ...] = ()
    model_provider: str = "deterministic"
    ollama_base_url: str | None = None
    ollama_model_id: str = "llama3.1:8b"
    litellm_base_url: str | None = None
    litellm_model_id: str = ""
    litellm_api_key: str | None = field(default=None, repr=False)
    # Claude through the Anthropic SDK, on the Claude API (provider "anthropic")
    # or in Amazon Bedrock (provider "bedrock"). Voice and chat answers use
    # anthropic_effort; the agent planner, which decides the actions, uses
    # anthropic_planner_effort. No key here means the SDK reads ANTHROPIC_API_KEY;
    # Bedrock signs with AWS credentials instead and needs bedrock_region. On
    # Bedrock, a model ID that is not Claude (apac.amazon.nova-pro-v1:0) is
    # served through the Converse API instead, and takes no effort setting.
    anthropic_model_id: str = ANTHROPIC_DEFAULT_MODEL_ID
    anthropic_api_key: str | None = field(default=None, repr=False)
    anthropic_effort: str = "low"
    anthropic_planner_effort: str = "medium"
    bedrock_region: str | None = None
    model_timeout_seconds: float = 8.0
    model_max_tokens: int = 800
    institution_connector_base_url: str | None = None
    institution_connector_source_id: str = "college_a_remote"
    institution_connector_institution_id: str = "college_a"
    institution_connector_display_name: str = "Configured institution connector"
    institution_connector_auth_token: str | None = field(default=None, repr=False)
    institution_connectors: tuple[InstitutionConnectorDefinition, ...] = ()
    connector_timeout_seconds: float = 5.0
    connector_max_response_bytes: int = 1_000_000
    connector_scope_attestation_required: bool = False
    demo_data_enabled: bool = True
    pdp_mode: str = "local"
    cerbos_url: str | None = None
    cerbos_policy_version: str = "guru-cerbos-v1"
    cerbos_timeout_seconds: float = 1.5
    cerbos_require_fresh: bool = True
    audit_fail_closed: bool = False
    audit_retention_days: int = 365
    otel_exporter_endpoint: str | None = None
    otel_exporter_timeout_seconds: float = 2.0
    web_search_provider: str = "disabled"
    web_search_endpoint: str = "https://api.tavily.com/search"
    web_search_api_key: str | None = field(default=None, repr=False)
    web_search_timeout_seconds: float = 8.0
    web_search_max_results: int = 5
    web_extract_timeout_seconds: float = 8.0
    web_extract_max_bytes: int = 1_000_000
    web_allowed_domains: tuple[str, ...] = tuple(sorted(DEFAULT_ALLOWED_DOMAINS))
    max_request_bytes: int = 1_000_000
    request_timeout_seconds: float = 30.0
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0
    edge_adapter: str = "disabled"
    edge_adapter_base_url: str | None = None
    edge_adapter_auth_token: str | None = field(default=None, repr=False)
    agent_gateway_url: str | None = None
    openfga_url: str | None = None
    livekit_url: str | None = None
    # Institutional data platform (ingestion, canonical database, agents, intelligence).
    # ``None`` means "not configured": on for development, off for production,
    # where a deployment opts in with SAFFRON_PLATFORM_ENABLED=true once PostgreSQL,
    # S3 and a queue are configured. ``__post_init__`` resolves it to a bool.
    platform_enabled: bool | None = None
    institution_database_url: str | None = field(default=None, repr=False)
    object_store_backend: str = "local"
    object_store_path: str = "./data/objects"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_prefix: str = ""
    job_queue: str = "inline"
    sqs_queue_url: str | None = None
    # A background job still ``running`` after this many seconds is treated as
    # interrupted (worker restart) and returned to the queue at boot.
    job_stale_seconds: int = 180  # a running job whose heartbeat is older than this lost its worker
    max_upload_bytes: int = 25_000_000
    ingestion_max_rows: int = 50_000
    ingestion_auto_commit: bool = True
    mapping_confidence_threshold: float = 0.8
    ocr_engine: str = "disabled"
    ocr_languages: str = "eng"
    email_provider: str = "outbox"
    email_sender: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_use_tls: bool = True
    email_allowed_domains: tuple[str, ...] = ()
    embedding_provider: str = "hashing"
    embedding_model_id: str = ""
    embedding_base_url: str | None = None
    embedding_api_key: str | None = field(default=None, repr=False)
    intelligence_search_provider: str = "disabled"
    intelligence_fetch_pages: bool = True
    intelligence_max_queries: int = 8
    intelligence_results_per_query: int = 5
    intelligence_crawler_contact: str = ""
    intelligence_map_enabled: bool = False
    intelligence_suppression_key: str | None = field(default=None, repr=False)
    intelligence_budgets: str = ""
    intelligence_tenant_share: float = 0.5
    intelligence_sources_per_tick: int = 25
    intelligence_connectors: tuple[str, ...] = ()
    intelligence_seed_groups: str = ""
    intelligence_entity_approvers: str = ""
    intelligence_authority_contacts: str = ""
    intelligence_investigations_per_day: int = 20
    # How long monitoring evidence and the internet map's closed work are kept (India's DPDP Act).
    intelligence_retention_days: int = 365
    intelligence_youtube_api_key: str | None = field(default=None, repr=False)
    intelligence_indiankanoon_token: str | None = field(default=None, repr=False)
    intelligence_certspotter_token: str | None = field(default=None, repr=False)
    agent_planner: str = "deterministic"
    approval_ttl_seconds: int = 900
    voice_agent_mode: str = "assistant"
    # A voice conversation stays open while the person keeps talking: it ends
    # after voice_idle_timeout_seconds of silence or voice_session_max_seconds
    # in all. Sessions live in the control database, so the request that opens
    # one and the WebSocket that uses it may reach different API tasks.
    voice_session_max_seconds: int = 1800
    voice_idle_timeout_seconds: int = 180
    voice_ticket_ttl_seconds: int = 60
    voice_max_active_sessions: int = 50
    voice_max_sessions_per_person: int = 2
    voice_utterances_per_minute: int = 20
    # Spoken replies: "browser" leaves speech to the browser's own voices;
    # "polly" speaks with Amazon Polly (Kajal, Indian English and Hindi), with
    # the browser voice as the fallback for Kannada or when Polly is unavailable.
    voice_tts_provider: str = "browser"
    voice_polly_voice_id: str = "Kajal"
    voice_polly_engine: str = "neural"
    voice_polly_region: str | None = None
    voice_tts_timeout_seconds: float = 4.0
    voice_tts_max_concurrency: int = 8
    voice_tts_max_chars_per_reply: int = 1500
    # Free conversation: greetings, general questions and follow-ups get a
    # spoken reply instead of a refusal; institutional figures still come only
    # from the tools. SAFFRON_CONVERSATION_MODEL_ID picks a faster Claude model for
    # these turns (the answer model is used otherwise).
    conversation_enabled: bool = True
    conversation_timeout_seconds: float = 8.0
    conversation_model_id: str = ""
    # Spoken replies are streamed: the first sentence is spoken while the rest
    # is still being written. The first text must arrive within
    # conversation_first_text_seconds and the whole reply within
    # conversation_stream_seconds (typed chat keeps conversation_timeout_seconds).
    conversation_first_text_seconds: float = 6.0
    conversation_stream_seconds: float = 20.0
    # A short spoken "one moment" when nothing has been said this long after
    # the person finished speaking; 0 turns it off.
    voice_filler_after_ms: int = 1800
    # Open-web search by voice or text ("search the internet for ..."), through
    # the SAFFRON_WEB_SEARCH_* Tavily settings, limited per person per day.
    assistant_web_search: bool = True
    web_searches_per_person_per_day: int = 25
    assistant_web_exclude_domains: tuple[str, ...] = ()
    # The client assistant is always served at ``/``. The developer platform
    # console at ``/console/`` is a separate app; ``None`` means "not configured":
    # served outside production, not served in production unless a deployment
    # opts in with SAFFRON_WEB_CONSOLE_ENABLED=true (ideally one that clients do not
    # reach). The API routes keep their own capability checks either way.
    web_console_enabled: bool | None = None
    # The open-task agent: when the planner's tools cannot do a job (a
    # presentation, a Word document, charts, a custom analysis), Claude reads
    # the records through the tool gateway as the person who asked, writes and
    # runs Python in a sandbox, and hands back the files as reports. Off unless
    # a deployment turns it on; needs a Claude provider and the data platform.
    open_task_enabled: bool = False
    open_task_model_id: str = OPEN_TASK_DEFAULT_MODEL_ID
    open_task_effort: str = "high"
    # One model turn may think for a while; the whole task has its own deadline.
    open_task_turn_timeout_seconds: float = 300.0
    open_task_deadline_seconds: float = 900.0
    open_task_max_turns: int = 30
    open_task_per_person_per_day: int = 10
    # Run tasks as background jobs (with a notification) when a thread or SQS queue is configured.
    open_task_background: bool = True
    # isolated (namespaces; the only mode production accepts) | auto | guarded (development only).
    open_task_sandbox: str = "isolated"
    open_task_run_timeout_seconds: float = 120.0
    open_task_memory_mb: int = 2048
    open_task_sandbox_dir: str | None = None
    open_task_hidden_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.platform_enabled is None:
            object.__setattr__(self, "platform_enabled", self.environment != "production")
        if self.web_console_enabled is None:
            object.__setattr__(self, "web_console_enabled", self.environment != "production")

    @classmethod
    def from_env(cls) -> "AppSettings":
        adopt_legacy_names()
        environment = os.getenv("SAFFRON_ENVIRONMENT", "development").strip().lower()
        control_url = os.getenv("CONTROL_DATABASE_URL") or _default_control_database_url()
        # An in-memory control plane is an ephemeral run (tests, CI); keep uploads in memory too.
        default_object_store = "memory" if control_url in {":memory:", "sqlite:///:memory:"} else "local"
        configured_origins = os.getenv("SAFFRON_ALLOWED_ORIGINS")
        default_origins = "" if environment == "production" else "http://localhost:5173"
        origins = tuple(item.strip() for item in (configured_origins or default_origins).split(",") if item.strip())
        return cls(
            app_name=os.getenv("SAFFRON_APP_NAME", "agentic-saffron-api"),
            version=os.getenv("SAFFRON_VERSION", "0.1.0"),
            environment=environment,
            allowed_origins=origins,
            dev_bearer_token=os.getenv("SAFFRON_DEV_BEARER_TOKEN", "dev-token"),
            control_database_url=os.getenv("CONTROL_DATABASE_URL", _default_control_database_url()),
            oidc_issuer_url=os.getenv("SAFFRON_OIDC_ISSUER_URL") or None,
            oidc_audience=os.getenv("SAFFRON_OIDC_AUDIENCE") or None,
            oidc_jwks_url=os.getenv("SAFFRON_OIDC_JWKS_URL") or None,
            oidc_algorithms=tuple(item.strip() for item in os.getenv("SAFFRON_OIDC_ALGORITHMS", "RS256").split(",") if item.strip()),
            oidc_browser_origins=_origins_from_env("SAFFRON_OIDC_BROWSER_ORIGINS", os.getenv("SAFFRON_OIDC_ISSUER_URL")),
            model_provider=os.getenv("SAFFRON_MODEL_PROVIDER", "deterministic").strip().lower(),
            ollama_base_url=os.getenv("SAFFRON_OLLAMA_BASE_URL") or None,
            ollama_model_id=os.getenv("SAFFRON_OLLAMA_MODEL_ID", "llama3.1:8b").strip(),
            litellm_base_url=os.getenv("SAFFRON_LITELLM_BASE_URL") or None,
            litellm_model_id=os.getenv("SAFFRON_LITELLM_MODEL_ID", "").strip(),
            litellm_api_key=os.getenv("SAFFRON_LITELLM_API_KEY") or None,
            anthropic_model_id=os.getenv("SAFFRON_ANTHROPIC_MODEL_ID", ANTHROPIC_DEFAULT_MODEL_ID).strip(),
            anthropic_api_key=os.getenv("SAFFRON_ANTHROPIC_API_KEY") or None,
            anthropic_effort=os.getenv("SAFFRON_ANTHROPIC_EFFORT", "low").strip().lower(),
            anthropic_planner_effort=os.getenv("SAFFRON_ANTHROPIC_PLANNER_EFFORT", "medium").strip().lower(),
            bedrock_region=(os.getenv("SAFFRON_BEDROCK_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip() or None,
            model_timeout_seconds=float(os.getenv("SAFFRON_MODEL_TIMEOUT_SECONDS", "8")),
            model_max_tokens=int(os.getenv("SAFFRON_MODEL_MAX_TOKENS", "800")),
            institution_connector_base_url=os.getenv("SAFFRON_INSTITUTION_CONNECTOR_BASE_URL") or None,
            institution_connector_source_id=os.getenv("SAFFRON_INSTITUTION_CONNECTOR_SOURCE_ID", "college_a_remote").strip(),
            institution_connector_institution_id=os.getenv("SAFFRON_INSTITUTION_CONNECTOR_INSTITUTION_ID", "college_a").strip(),
            institution_connector_display_name=os.getenv("SAFFRON_INSTITUTION_CONNECTOR_DISPLAY_NAME", "Configured institution connector").strip(),
            institution_connector_auth_token=os.getenv("SAFFRON_INSTITUTION_CONNECTOR_AUTH_TOKEN") or None,
            institution_connectors=parse_institution_connectors(
                os.getenv("SAFFRON_INSTITUTION_CONNECTORS"),
                environment=os.environ,
            ),
            connector_timeout_seconds=float(os.getenv("SAFFRON_CONNECTOR_TIMEOUT_SECONDS", "5")),
            connector_max_response_bytes=int(os.getenv("SAFFRON_CONNECTOR_MAX_RESPONSE_BYTES", "1000000")),
            connector_scope_attestation_required=_bool_env("SAFFRON_CONNECTOR_SCOPE_ATTESTATION_REQUIRED", environment == "production"),
            demo_data_enabled=_bool_env("SAFFRON_ENABLE_DEMO_DATA", environment != "production"),
            pdp_mode=os.getenv("SAFFRON_PDP_MODE", "local").strip().lower(),
            cerbos_url=os.getenv("SAFFRON_CERBOS_URL") or None,
            cerbos_policy_version=os.getenv("SAFFRON_CERBOS_POLICY_VERSION", "guru-cerbos-v1").strip(),
            cerbos_timeout_seconds=float(os.getenv("SAFFRON_CERBOS_TIMEOUT_SECONDS", "1.5")),
            cerbos_require_fresh=_bool_env("SAFFRON_CERBOS_REQUIRE_FRESH", True),
            audit_fail_closed=_bool_env("SAFFRON_AUDIT_FAIL_CLOSED", environment == "production"),
            audit_retention_days=int(os.getenv("SAFFRON_AUDIT_RETENTION_DAYS", "365")),
            otel_exporter_endpoint=os.getenv("SAFFRON_OTEL_EXPORTER_ENDPOINT") or None,
            otel_exporter_timeout_seconds=float(os.getenv("SAFFRON_OTEL_EXPORTER_TIMEOUT_SECONDS", "2")),
            web_search_provider=os.getenv("SAFFRON_WEB_SEARCH_PROVIDER", "disabled").strip().lower(),
            web_search_endpoint=os.getenv("SAFFRON_WEB_SEARCH_ENDPOINT", "https://api.tavily.com/search").strip(),
            web_search_api_key=os.getenv("SAFFRON_WEB_SEARCH_API_KEY") or None,
            web_search_timeout_seconds=float(os.getenv("SAFFRON_WEB_SEARCH_TIMEOUT_SECONDS", "8")),
            web_search_max_results=int(os.getenv("SAFFRON_WEB_SEARCH_MAX_RESULTS", "5")),
            web_extract_timeout_seconds=float(os.getenv("SAFFRON_WEB_EXTRACT_TIMEOUT_SECONDS", "8")),
            web_extract_max_bytes=int(os.getenv("SAFFRON_WEB_EXTRACT_MAX_BYTES", "1000000")),
            web_allowed_domains=tuple(item.strip().lower().rstrip(".") for item in os.getenv("SAFFRON_WEB_ALLOWED_DOMAINS", ",".join(sorted(DEFAULT_ALLOWED_DOMAINS))).split(",") if item.strip()),
            max_request_bytes=int(os.getenv("SAFFRON_MAX_REQUEST_BYTES", "1000000")),
            request_timeout_seconds=float(os.getenv("SAFFRON_REQUEST_TIMEOUT_SECONDS", "30")),
            rate_limit_requests=int(os.getenv("SAFFRON_RATE_LIMIT_REQUESTS", "120")),
            rate_limit_window_seconds=float(os.getenv("SAFFRON_RATE_LIMIT_WINDOW_SECONDS", "60")),
            edge_adapter=os.getenv("SAFFRON_EDGE_ADAPTER", "disabled").strip().lower(),
            edge_adapter_base_url=os.getenv("SAFFRON_EDGE_ADAPTER_BASE_URL") or None,
            edge_adapter_auth_token=os.getenv("SAFFRON_EDGE_ADAPTER_AUTH_TOKEN") or None,
            agent_gateway_url=os.getenv("SAFFRON_AGENT_GATEWAY_URL") or None,
            openfga_url=os.getenv("SAFFRON_OPENFGA_URL") or None,
            livekit_url=os.getenv("SAFFRON_LIVEKIT_URL") or None,
            platform_enabled=None if os.getenv("SAFFRON_PLATFORM_ENABLED") is None else _bool_env("SAFFRON_PLATFORM_ENABLED", False),
            institution_database_url=os.getenv("INSTITUTION_DATABASE_URL") or None,
            object_store_backend=(os.getenv("SAFFRON_OBJECT_STORE") or default_object_store).strip().lower(),
            object_store_path=os.getenv("SAFFRON_OBJECT_STORE_PATH", "./data/objects").strip(),
            s3_bucket=os.getenv("SAFFRON_S3_BUCKET") or None,
            s3_region=os.getenv("SAFFRON_S3_REGION") or os.getenv("AWS_REGION") or None,
            s3_prefix=os.getenv("SAFFRON_S3_PREFIX", "").strip(),
            job_queue=os.getenv("SAFFRON_JOB_QUEUE", "inline").strip().lower(),
            sqs_queue_url=os.getenv("SAFFRON_SQS_QUEUE_URL") or None,
            job_stale_seconds=int(os.getenv("SAFFRON_JOB_STALE_SECONDS", "180")),
            max_upload_bytes=int(os.getenv("SAFFRON_MAX_UPLOAD_BYTES", "25000000")),
            ingestion_max_rows=int(os.getenv("SAFFRON_INGESTION_MAX_ROWS", "50000")),
            ingestion_auto_commit=_bool_env("SAFFRON_INGESTION_AUTO_COMMIT", True),
            mapping_confidence_threshold=float(os.getenv("SAFFRON_MAPPING_CONFIDENCE_THRESHOLD", "0.8")),
            ocr_engine=os.getenv("SAFFRON_OCR_ENGINE", "disabled").strip().lower(),
            ocr_languages=os.getenv("SAFFRON_OCR_LANGUAGES", "eng").strip(),
            email_provider=os.getenv("SAFFRON_EMAIL_PROVIDER", "outbox").strip().lower(),
            email_sender=os.getenv("SAFFRON_EMAIL_SENDER") or None,
            smtp_host=os.getenv("SAFFRON_SMTP_HOST") or None,
            smtp_port=int(os.getenv("SAFFRON_SMTP_PORT", "587")),
            smtp_username=os.getenv("SAFFRON_SMTP_USERNAME") or None,
            smtp_password=os.getenv("SAFFRON_SMTP_PASSWORD") or None,
            smtp_use_tls=_bool_env("SAFFRON_SMTP_USE_TLS", True),
            email_allowed_domains=tuple(item.strip().lower() for item in os.getenv("SAFFRON_EMAIL_ALLOWED_DOMAINS", "").split(",") if item.strip()),
            embedding_provider=os.getenv("SAFFRON_EMBEDDING_PROVIDER", "hashing").strip().lower(),
            embedding_model_id=os.getenv("SAFFRON_EMBEDDING_MODEL_ID", "").strip(),
            embedding_base_url=os.getenv("SAFFRON_EMBEDDING_BASE_URL") or None,
            embedding_api_key=os.getenv("SAFFRON_EMBEDDING_API_KEY") or None,
            intelligence_search_provider=os.getenv("SAFFRON_INTELLIGENCE_SEARCH_PROVIDER", "disabled").strip().lower(),
            intelligence_fetch_pages=_bool_env("SAFFRON_INTELLIGENCE_FETCH_PAGES", True),
            intelligence_max_queries=int(os.getenv("SAFFRON_INTELLIGENCE_MAX_QUERIES", "8")),
            intelligence_results_per_query=int(os.getenv("SAFFRON_INTELLIGENCE_RESULTS_PER_QUERY", "5")),
            intelligence_crawler_contact=os.getenv("SAFFRON_INTELLIGENCE_CRAWLER_CONTACT", "").strip(),
            intelligence_map_enabled=_bool_env("SAFFRON_INTELLIGENCE_MAP_ENABLED", False),
            intelligence_suppression_key=os.getenv("SAFFRON_INTELLIGENCE_SUPPRESSION_KEY") or None,
            intelligence_budgets=os.getenv("SAFFRON_INTELLIGENCE_BUDGETS", "").strip(),
            intelligence_tenant_share=float(os.getenv("SAFFRON_INTELLIGENCE_TENANT_SHARE", "0.5")),
            intelligence_sources_per_tick=int(os.getenv("SAFFRON_INTELLIGENCE_SOURCES_PER_TICK", "25")),
            intelligence_seed_groups=os.getenv("SAFFRON_INTELLIGENCE_SEED_GROUPS", "").strip(),
            intelligence_entity_approvers=os.getenv("SAFFRON_INTELLIGENCE_ENTITY_APPROVERS", "").strip(),
            intelligence_authority_contacts=os.getenv("SAFFRON_INTELLIGENCE_AUTHORITY_CONTACTS", "").strip(),
            intelligence_investigations_per_day=int(os.getenv("SAFFRON_INTELLIGENCE_INVESTIGATIONS_PER_DAY", "20")),
            intelligence_retention_days=int(os.getenv("SAFFRON_INTELLIGENCE_RETENTION_DAYS", "365")),
            intelligence_connectors=tuple(item.strip().lower() for item in os.getenv("SAFFRON_INTELLIGENCE_CONNECTORS", "").split(",") if item.strip()),
            intelligence_youtube_api_key=os.getenv("SAFFRON_INTELLIGENCE_YOUTUBE_API_KEY") or None,
            intelligence_indiankanoon_token=os.getenv("SAFFRON_INTELLIGENCE_INDIANKANOON_TOKEN") or None,
            intelligence_certspotter_token=os.getenv("SAFFRON_INTELLIGENCE_CERTSPOTTER_TOKEN") or None,
            agent_planner=os.getenv("SAFFRON_AGENT_PLANNER", "deterministic").strip().lower(),
            approval_ttl_seconds=int(os.getenv("SAFFRON_APPROVAL_TTL_SECONDS", "900")),
            voice_agent_mode=os.getenv("SAFFRON_VOICE_AGENT_MODE", "assistant").strip().lower(),
            voice_session_max_seconds=int(os.getenv("SAFFRON_VOICE_SESSION_MAX_SECONDS", "1800")),
            voice_idle_timeout_seconds=int(os.getenv("SAFFRON_VOICE_IDLE_TIMEOUT_SECONDS", "180")),
            voice_ticket_ttl_seconds=int(os.getenv("SAFFRON_VOICE_TICKET_TTL_SECONDS", "60")),
            voice_max_active_sessions=int(os.getenv("SAFFRON_VOICE_MAX_ACTIVE_SESSIONS", "50")),
            voice_max_sessions_per_person=int(os.getenv("SAFFRON_VOICE_MAX_SESSIONS_PER_PERSON", "2")),
            voice_utterances_per_minute=int(os.getenv("SAFFRON_VOICE_UTTERANCES_PER_MINUTE", "20")),
            voice_tts_provider=os.getenv("SAFFRON_VOICE_TTS_PROVIDER", "browser").strip().lower(),
            voice_polly_voice_id=os.getenv("SAFFRON_VOICE_POLLY_VOICE_ID", "Kajal").strip(),
            voice_polly_engine=os.getenv("SAFFRON_VOICE_POLLY_ENGINE", "neural").strip().lower(),
            voice_polly_region=(os.getenv("SAFFRON_VOICE_POLLY_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip() or None,
            voice_tts_timeout_seconds=float(os.getenv("SAFFRON_VOICE_TTS_TIMEOUT_SECONDS", "4")),
            voice_tts_max_concurrency=int(os.getenv("SAFFRON_VOICE_TTS_MAX_CONCURRENCY", "8")),
            voice_tts_max_chars_per_reply=int(os.getenv("SAFFRON_VOICE_TTS_MAX_CHARS_PER_REPLY", "1500")),
            conversation_enabled=_bool_env("SAFFRON_CONVERSATION_ENABLED", True),
            conversation_timeout_seconds=float(os.getenv("SAFFRON_CONVERSATION_TIMEOUT_SECONDS", "8")),
            conversation_model_id=os.getenv("SAFFRON_CONVERSATION_MODEL_ID", "").strip(),
            conversation_first_text_seconds=float(os.getenv("SAFFRON_CONVERSATION_FIRST_TEXT_SECONDS", "6")),
            conversation_stream_seconds=float(os.getenv("SAFFRON_CONVERSATION_STREAM_SECONDS", "20")),
            voice_filler_after_ms=int(os.getenv("SAFFRON_VOICE_FILLER_AFTER_MS", "1800")),
            assistant_web_search=_bool_env("SAFFRON_ASSISTANT_WEB_SEARCH", True),
            web_searches_per_person_per_day=int(os.getenv("SAFFRON_WEB_SEARCHES_PER_PERSON_PER_DAY", "25")),
            assistant_web_exclude_domains=tuple(item.strip().lower().rstrip(".") for item in os.getenv("SAFFRON_ASSISTANT_WEB_EXCLUDE_DOMAINS", "").split(",") if item.strip()),
            web_console_enabled=None if os.getenv("SAFFRON_WEB_CONSOLE_ENABLED") is None else _bool_env("SAFFRON_WEB_CONSOLE_ENABLED", False),
            open_task_enabled=_bool_env("SAFFRON_OPEN_TASK_ENABLED", False),
            open_task_model_id=os.getenv("SAFFRON_OPEN_TASK_MODEL_ID", OPEN_TASK_DEFAULT_MODEL_ID).strip(),
            open_task_effort=os.getenv("SAFFRON_OPEN_TASK_EFFORT", "high").strip().lower(),
            open_task_turn_timeout_seconds=float(os.getenv("SAFFRON_OPEN_TASK_TURN_TIMEOUT_SECONDS", "300")),
            open_task_deadline_seconds=float(os.getenv("SAFFRON_OPEN_TASK_DEADLINE_SECONDS", "900")),
            open_task_max_turns=int(os.getenv("SAFFRON_OPEN_TASK_MAX_TURNS", "30")),
            open_task_per_person_per_day=int(os.getenv("SAFFRON_OPEN_TASK_PER_PERSON_PER_DAY", "10")),
            open_task_background=_bool_env("SAFFRON_OPEN_TASK_BACKGROUND", True),
            open_task_sandbox=os.getenv("SAFFRON_OPEN_TASK_SANDBOX", "isolated").strip().lower(),
            open_task_run_timeout_seconds=float(os.getenv("SAFFRON_OPEN_TASK_RUN_TIMEOUT_SECONDS", "120")),
            open_task_memory_mb=int(os.getenv("SAFFRON_OPEN_TASK_MEMORY_MB", "2048")),
            open_task_sandbox_dir=(os.getenv("SAFFRON_OPEN_TASK_SANDBOX_DIR") or "").strip() or None,
            open_task_hidden_paths=tuple(item.strip() for item in os.getenv("SAFFRON_OPEN_TASK_HIDDEN_PATHS", "").split(",") if item.strip()),
        )

    def resolved_institution_database_url(self) -> str:
        """The canonical institution database; defaults follow the control-plane choice."""

        if self.institution_database_url:
            return self.institution_database_url
        control = self.control_database_url or ""
        if control in {":memory:", "sqlite:///:memory:"} or not control:
            return ":memory:"
        if control.startswith(("postgresql://", "postgres://")):
            return control
        return "sqlite:///./data/institution_data.db"

    def platform_production_ready(self) -> bool:
        return (
            self.platform_enabled
            and self.resolved_institution_database_url().startswith(("postgresql://", "postgres://"))
            and self.object_store_backend == "s3"
            and self.job_queue in {"thread", "sqs"}
        )

    def configured_institution_connectors(self) -> tuple[InstitutionConnectorDefinition, ...]:
        """Return all connector definitions, including the legacy single entry.

        The original single-connector environment variables remain supported so
        existing deployments do not change behavior when the multi-connector
        registry is not configured.
        """

        definitions = list(self.institution_connectors)
        if self.institution_connector_base_url:
            legacy = InstitutionConnectorDefinition(
                source_id=self.institution_connector_source_id,
                institution_id=self.institution_connector_institution_id,
                display_name=self.institution_connector_display_name,
                base_url=self.institution_connector_base_url,
                auth_token=self.institution_connector_auth_token,
                scope_attestation_required=self.connector_scope_attestation_required,
            )
            if any(item.source_id == legacy.source_id for item in definitions):
                raise ValueError(f"institution connector source ID is duplicated: {legacy.source_id}")
            definitions.append(legacy)
        return tuple(definitions)

    def intelligence_seed_group_map(self) -> dict[str, tuple[str, ...]]:
        """Which sweep groups are each institution's own (SAFFRON_INTELLIGENCE_SEED_GROUPS, JSON)."""

        if not self.intelligence_seed_groups:
            return {}
        import json

        try:
            parsed = json.loads(self.intelligence_seed_groups)
        except ValueError as exc:
            raise ValueError("SAFFRON_INTELLIGENCE_SEED_GROUPS must be a JSON object of institution ID to a list of sweep group labels") from exc
        if not isinstance(parsed, dict) or not all(isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value) for key, value in parsed.items()):
            raise ValueError("SAFFRON_INTELLIGENCE_SEED_GROUPS must be a JSON object of institution ID to a list of sweep group labels")
        return {key: tuple(value) for key, value in parsed.items()}

    def intelligence_entity_approver_map(self) -> dict[str, tuple[str, ...]]:
        """Who may decide for another group's entities (SAFFRON_INTELLIGENCE_ENTITY_APPROVERS, JSON of sweep group to principal IDs).

        Without an entry, the institution's managers can only dismiss review
        items about that group's entities: which of the Swamiji's accounts is
        real is for the Math or trust IT office to say.
        """

        return _group_lists(self.intelligence_entity_approvers, "SAFFRON_INTELLIGENCE_ENTITY_APPROVERS", "principal IDs")

    def intelligence_authority_contact_map(self) -> dict[str, tuple[str, ...]]:
        """Where another group's security incidents are emailed (SAFFRON_INTELLIGENCE_AUTHORITY_CONTACTS, JSON of sweep group to addresses)."""

        contacts = _group_lists(self.intelligence_authority_contacts, "SAFFRON_INTELLIGENCE_AUTHORITY_CONTACTS", "email addresses")
        invalid = [address for addresses in contacts.values() for address in addresses if not _EMAIL_ADDRESS.fullmatch(address)]
        if invalid:
            raise ValueError(f"SAFFRON_INTELLIGENCE_AUTHORITY_CONTACTS lists something that is not an email address: {invalid[0]}")
        return contacts

    def intelligence_budget_caps(self) -> dict[str, float]:
        """Daily platform-wide caps per connector budget, defaults overridden by SAFFRON_INTELLIGENCE_BUDGETS (JSON)."""

        from ..internet_intelligence.map.engine import DEFAULT_BUDGETS

        caps = dict(DEFAULT_BUDGETS)
        if self.intelligence_budgets:
            import json

            try:
                overrides = json.loads(self.intelligence_budgets)
            except ValueError as exc:
                raise ValueError("SAFFRON_INTELLIGENCE_BUDGETS must be a JSON object of budget name to daily units") from exc
            if not isinstance(overrides, dict) or not all(isinstance(key, str) and isinstance(value, (int, float)) and value >= 0 for key, value in overrides.items()):
                raise ValueError("SAFFRON_INTELLIGENCE_BUDGETS must be a JSON object of budget name to daily units")
            caps.update({key: float(value) for key, value in overrides.items()})
        return caps

    def ensure_safe_for_production(self) -> None:
        if self.max_request_bytes <= 0:
            raise ValueError("SAFFRON_MAX_REQUEST_BYTES must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("SAFFRON_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.rate_limit_requests <= 0:
            raise ValueError("SAFFRON_RATE_LIMIT_REQUESTS must be positive")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("SAFFRON_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.model_provider not in {"deterministic", "ollama", "litellm", "anthropic", "bedrock"}:
            raise ValueError("SAFFRON_MODEL_PROVIDER must be deterministic, ollama, litellm, anthropic, or bedrock")
        if self.model_timeout_seconds <= 0:
            raise ValueError("SAFFRON_MODEL_TIMEOUT_SECONDS must be positive")
        if self.model_max_tokens <= 0:
            raise ValueError("SAFFRON_MODEL_MAX_TOKENS must be positive")
        if self.model_provider == "ollama" and not self.ollama_base_url:
            raise ValueError("SAFFRON_OLLAMA_BASE_URL is required when SAFFRON_MODEL_PROVIDER=ollama")
        if self.model_provider == "litellm" and not self.litellm_model_id:
            raise ValueError("SAFFRON_LITELLM_MODEL_ID is required when SAFFRON_MODEL_PROVIDER=litellm")
        if self.model_provider in {"anthropic", "bedrock"} and not self.anthropic_model_id:
            raise ValueError(f"SAFFRON_ANTHROPIC_MODEL_ID must not be blank when SAFFRON_MODEL_PROVIDER={self.model_provider}")
        if self.model_provider == "bedrock" and not self.bedrock_region:
            raise ValueError("SAFFRON_BEDROCK_REGION (or AWS_REGION) is required when SAFFRON_MODEL_PROVIDER=bedrock")
        for name, effort in (("SAFFRON_ANTHROPIC_EFFORT", self.anthropic_effort), ("SAFFRON_ANTHROPIC_PLANNER_EFFORT", self.anthropic_planner_effort)):
            if effort not in ANTHROPIC_EFFORT_LEVELS:
                raise ValueError(f"{name} must be one of {', '.join(ANTHROPIC_EFFORT_LEVELS)}")
        if self.pdp_mode not in {"local", "cerbos"}:
            raise ValueError("SAFFRON_PDP_MODE must be local or cerbos")
        if self.cerbos_timeout_seconds <= 0:
            raise ValueError("SAFFRON_CERBOS_TIMEOUT_SECONDS must be positive")
        if not self.cerbos_policy_version.strip():
            raise ValueError("SAFFRON_CERBOS_POLICY_VERSION must not be blank")
        if self.pdp_mode == "cerbos":
            if not self.cerbos_url:
                raise ValueError("SAFFRON_CERBOS_URL is required when SAFFRON_PDP_MODE=cerbos")
            parsed_cerbos = urlparse(self.cerbos_url)
            if parsed_cerbos.scheme not in {"http", "https"} or not parsed_cerbos.netloc:
                raise ValueError("SAFFRON_CERBOS_URL must be an absolute HTTP(S) URL")
        if self.connector_timeout_seconds <= 0 or self.connector_max_response_bytes <= 0:
            raise ValueError("connector limits must be positive")
        if not 1 <= self.audit_retention_days <= 3650:
            raise ValueError("SAFFRON_AUDIT_RETENTION_DAYS must be between 1 and 3650")
        if not 30 <= self.intelligence_retention_days <= 3650:
            raise ValueError("SAFFRON_INTELLIGENCE_RETENTION_DAYS must be between 30 and 3650")
        if self.otel_exporter_timeout_seconds <= 0:
            raise ValueError("SAFFRON_OTEL_EXPORTER_TIMEOUT_SECONDS must be positive")
        if self.otel_exporter_endpoint:
            parsed_otel = urlparse(self.otel_exporter_endpoint)
            if parsed_otel.scheme not in {"http", "https"} or not parsed_otel.netloc:
                raise ValueError("SAFFRON_OTEL_EXPORTER_ENDPOINT must be an absolute HTTP(S) URL")
        if self.web_search_provider not in {"disabled", "tavily"}:
            raise ValueError("SAFFRON_WEB_SEARCH_PROVIDER must be disabled or tavily")
        if self.web_search_timeout_seconds <= 0:
            raise ValueError("SAFFRON_WEB_SEARCH_TIMEOUT_SECONDS must be positive")
        if self.web_extract_timeout_seconds <= 0:
            raise ValueError("SAFFRON_WEB_EXTRACT_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.web_search_max_results <= 10:
            raise ValueError("SAFFRON_WEB_SEARCH_MAX_RESULTS must be between 1 and 10")
        if self.web_extract_max_bytes <= 0:
            raise ValueError("SAFFRON_WEB_EXTRACT_MAX_BYTES must be positive")
        if not self.web_allowed_domains:
            raise ValueError("SAFFRON_WEB_ALLOWED_DOMAINS must contain at least one hostname")
        for domain in self.web_allowed_domains:
            normalized_domain = domain.strip().lower().rstrip(".")
            if not normalized_domain or any(character in normalized_domain for character in "/?#:"):
                raise ValueError("SAFFRON_WEB_ALLOWED_DOMAINS must contain hostnames, not URLs")
            if urlparse(f"https://{normalized_domain}").hostname != normalized_domain:
                raise ValueError("SAFFRON_WEB_ALLOWED_DOMAINS contains an invalid hostname")
        if self.web_search_provider == "tavily":
            if not self.web_search_api_key:
                raise ValueError("SAFFRON_WEB_SEARCH_API_KEY is required when SAFFRON_WEB_SEARCH_PROVIDER=tavily")
            parsed_search_url = urlparse(self.web_search_endpoint)
            if parsed_search_url.scheme not in {"http", "https"} or not parsed_search_url.netloc:
                raise ValueError("SAFFRON_WEB_SEARCH_ENDPOINT must be an absolute HTTP(S) URL")
            if parsed_search_url.username or parsed_search_url.password or parsed_search_url.query or parsed_search_url.fragment:
                raise ValueError("SAFFRON_WEB_SEARCH_ENDPOINT must not contain credentials, query, or fragment data")
        for field_name in ("institution_connector_source_id", "institution_connector_institution_id", "institution_connector_display_name"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.institution_connector_base_url:
            parsed_connector_url = urlparse(self.institution_connector_base_url)
            if parsed_connector_url.scheme not in {"http", "https"} or not parsed_connector_url.netloc:
                raise ValueError("SAFFRON_INSTITUTION_CONNECTOR_BASE_URL must be an absolute HTTP(S) URL")
        self.configured_institution_connectors()
        if self.edge_adapter not in {"disabled", "openedx", "moodle"}:
            raise ValueError("SAFFRON_EDGE_ADAPTER must be disabled, openedx, or moodle")
        if self.edge_adapter != "disabled" and not self.edge_adapter_base_url:
            raise ValueError("SAFFRON_EDGE_ADAPTER_BASE_URL is required when an edge adapter is enabled")
        if self.edge_adapter != "disabled" and not self.edge_adapter_auth_token:
            raise ValueError("SAFFRON_EDGE_ADAPTER_AUTH_TOKEN is required when an edge adapter is enabled")
        for field_name in ("edge_adapter_base_url", "agent_gateway_url", "openfga_url", "livekit_url"):
            endpoint = getattr(self, field_name)
            if endpoint:
                parsed_endpoint = urlparse(endpoint)
                if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
                    raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
                if parsed_endpoint.username or parsed_endpoint.password or parsed_endpoint.query or parsed_endpoint.fragment:
                    raise ValueError(f"{field_name} must not contain credentials, query, or fragment data")
        self._validate_platform()
        self._validate_open_task()
        if self.environment != "production":
            return
        if self.web_search_provider == "tavily" and urlparse(self.web_search_endpoint).scheme != "https":
            raise ValueError("production requires the public-web search provider to use HTTPS")
        if self.dev_bearer_token == "dev-token":
            raise ValueError("the development bearer token must be replaced in production")
        if not self.allowed_origins:
            raise ValueError("SAFFRON_ALLOWED_ORIGINS must be set in production")
        if not self.control_database_url or not self.control_database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires CONTROL_DATABASE_URL to use PostgreSQL")
        if not self.oidc_issuer_url or not self.oidc_audience or not self.oidc_jwks_url:
            raise ValueError("production requires OIDC issuer, audience, and JWKS URL settings")
        allowed_algorithms = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
        if not self.oidc_algorithms or any(item not in allowed_algorithms for item in self.oidc_algorithms):
            raise ValueError("production requires an explicit safe OIDC signing algorithm")
        if self.demo_data_enabled:
            raise ValueError("production cannot enable deterministic demo data")
        configured_connectors = self.configured_institution_connectors()
        if not configured_connectors and not self.platform_production_ready():
            raise ValueError(
                "production requires SAFFRON_INSTITUTION_CONNECTOR_BASE_URL or SAFFRON_INSTITUTION_CONNECTORS, or a data platform with a PostgreSQL "
                "INSTITUTION_DATABASE_URL, SAFFRON_OBJECT_STORE=s3, and SAFFRON_JOB_QUEUE=thread|sqs"
            )
        for connector in configured_connectors:
            if urlparse(connector.base_url).scheme != "https":
                raise ValueError("production requires institutional connectors to use HTTPS")
            if not connector.auth_token:
                raise ValueError(f"production requires an auth token for connector {connector.source_id}")
            if not connector.scope_attestation_required:
                raise ValueError(f"production requires scope attestation for connector {connector.source_id}")
        if self.pdp_mode != "cerbos" or not self.cerbos_url:
            raise ValueError("production requires a Cerbos PDP")
        if urlparse(self.cerbos_url).scheme != "https":
            raise ValueError("production requires Cerbos over HTTPS")
        if not self.connector_scope_attestation_required:
            raise ValueError("production requires connector scope attestation")
        if self.model_provider == "deterministic":
            raise ValueError("production cannot use the deterministic provider")
        if not self.audit_fail_closed:
            raise ValueError("production requires fail-closed audit handling")
        if self.platform_enabled:
            if not self.resolved_institution_database_url().startswith(("postgresql://", "postgres://")):
                raise ValueError("production requires INSTITUTION_DATABASE_URL to use PostgreSQL (or SAFFRON_PLATFORM_ENABLED=false)")
            if self.object_store_backend != "s3":
                raise ValueError("production requires SAFFRON_OBJECT_STORE=s3 for the data platform")
            if self.job_queue == "inline":
                raise ValueError("production requires SAFFRON_JOB_QUEUE=thread or sqs")
            if self.email_provider == "smtp" and not self.smtp_use_tls:
                raise ValueError("production requires SAFFRON_SMTP_USE_TLS=true")
        if self.open_task_enabled and self.open_task_sandbox != "isolated":
            # Without namespaces nothing but an audit hook stands between model-written code and the host.
            raise ValueError("production requires SAFFRON_OPEN_TASK_SANDBOX=isolated when the open-task agent is enabled")

    def _validate_open_task(self) -> None:
        if not self.open_task_enabled:
            return
        if self.model_provider not in {"anthropic", "bedrock"}:
            raise ValueError("SAFFRON_OPEN_TASK_ENABLED needs SAFFRON_MODEL_PROVIDER=anthropic or bedrock")
        if not self.platform_enabled:
            raise ValueError("SAFFRON_OPEN_TASK_ENABLED needs the data platform (SAFFRON_PLATFORM_ENABLED)")
        if not self.open_task_model_id:
            raise ValueError("SAFFRON_OPEN_TASK_MODEL_ID must not be blank")
        if self.open_task_effort not in ANTHROPIC_EFFORT_LEVELS:
            raise ValueError(f"SAFFRON_OPEN_TASK_EFFORT must be one of {', '.join(ANTHROPIC_EFFORT_LEVELS)}")
        if self.open_task_sandbox not in {"isolated", "auto", "guarded"}:
            raise ValueError("SAFFRON_OPEN_TASK_SANDBOX must be isolated, auto, or guarded")
        for name, value in (
            ("SAFFRON_OPEN_TASK_TURN_TIMEOUT_SECONDS", self.open_task_turn_timeout_seconds), ("SAFFRON_OPEN_TASK_DEADLINE_SECONDS", self.open_task_deadline_seconds),
            ("SAFFRON_OPEN_TASK_MAX_TURNS", self.open_task_max_turns), ("SAFFRON_OPEN_TASK_PER_PERSON_PER_DAY", self.open_task_per_person_per_day),
            ("SAFFRON_OPEN_TASK_RUN_TIMEOUT_SECONDS", self.open_task_run_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.open_task_memory_mb < 256:
            raise ValueError("SAFFRON_OPEN_TASK_MEMORY_MB must be at least 256")

    def _validate_platform(self) -> None:
        if self.object_store_backend not in {"memory", "local", "s3"}:
            raise ValueError("SAFFRON_OBJECT_STORE must be memory, local, or s3")
        if self.object_store_backend == "s3" and not self.s3_bucket:
            raise ValueError("SAFFRON_S3_BUCKET is required when SAFFRON_OBJECT_STORE=s3")
        if self.job_queue not in {"inline", "thread", "sqs"}:
            raise ValueError("SAFFRON_JOB_QUEUE must be inline, thread, or sqs")
        if self.job_queue == "sqs" and not self.sqs_queue_url:
            raise ValueError("SAFFRON_SQS_QUEUE_URL is required when SAFFRON_JOB_QUEUE=sqs")
        if self.job_stale_seconds <= 0:
            raise ValueError("SAFFRON_JOB_STALE_SECONDS must be positive")
        if self.max_upload_bytes <= 0 or self.ingestion_max_rows <= 0:
            raise ValueError("upload and ingestion limits must be positive")
        if not 0.5 <= self.mapping_confidence_threshold <= 1.0:
            raise ValueError("SAFFRON_MAPPING_CONFIDENCE_THRESHOLD must be between 0.5 and 1.0")
        if self.ocr_engine not in {"disabled", "tesseract", "textract"}:
            raise ValueError("SAFFRON_OCR_ENGINE must be disabled, tesseract, or textract")
        if self.email_provider not in {"outbox", "smtp", "ses"}:
            raise ValueError("SAFFRON_EMAIL_PROVIDER must be outbox, smtp, or ses")
        if self.email_provider in {"smtp", "ses"} and not self.email_sender:
            raise ValueError("SAFFRON_EMAIL_SENDER is required for smtp or ses email delivery")
        if self.email_provider == "smtp" and not self.smtp_host:
            raise ValueError("SAFFRON_SMTP_HOST is required when SAFFRON_EMAIL_PROVIDER=smtp")
        if self.embedding_provider not in {"hashing", "ollama", "openai_compatible"}:
            raise ValueError("SAFFRON_EMBEDDING_PROVIDER must be hashing, ollama, or openai_compatible")
        if self.embedding_provider != "hashing" and not self.embedding_base_url:
            raise ValueError("SAFFRON_EMBEDDING_BASE_URL is required for remote embedding providers")
        if self.intelligence_search_provider not in {"disabled", "tavily"}:
            raise ValueError("SAFFRON_INTELLIGENCE_SEARCH_PROVIDER must be disabled or tavily")
        if self.intelligence_search_provider == "tavily" and not self.web_search_api_key:
            raise ValueError("SAFFRON_WEB_SEARCH_API_KEY is required when SAFFRON_INTELLIGENCE_SEARCH_PROVIDER=tavily")
        if not 1 <= self.intelligence_max_queries <= 20 or not 1 <= self.intelligence_results_per_query <= 20:
            raise ValueError("intelligence query limits must be between 1 and 20")
        self.intelligence_budget_caps()
        self.intelligence_seed_group_map()
        self.intelligence_entity_approver_map()
        self.intelligence_authority_contact_map()
        if not 0 <= self.intelligence_investigations_per_day <= 1000:
            raise ValueError("SAFFRON_INTELLIGENCE_INVESTIGATIONS_PER_DAY must be between 0 and 1000")
        if not 0 < self.intelligence_tenant_share <= 1:
            raise ValueError("SAFFRON_INTELLIGENCE_TENANT_SHARE must be greater than 0 and at most 1")
        if not 1 <= self.intelligence_sources_per_tick <= 500:
            raise ValueError("SAFFRON_INTELLIGENCE_SOURCES_PER_TICK must be between 1 and 500")
        unknown = sorted(set(self.intelligence_connectors) - set(OPTIONAL_CONNECTORS))
        if unknown:
            raise ValueError(f"SAFFRON_INTELLIGENCE_CONNECTORS names unknown connectors: {', '.join(unknown)} (known: {', '.join(OPTIONAL_CONNECTORS)})")
        if {"search", "spam_probe", "google_play_search"} & set(self.intelligence_connectors) and self.intelligence_search_provider == "disabled":
            raise ValueError("the search, spam_probe and google_play_search connectors need SAFFRON_INTELLIGENCE_SEARCH_PROVIDER")
        if "openstreetmap" in self.intelligence_connectors and not self.intelligence_crawler_contact:
            # Nominatim's usage policy asks every application to identify itself with a way to reach its operator.
            raise ValueError("the openstreetmap connector needs SAFFRON_INTELLIGENCE_CRAWLER_CONTACT (Nominatim's usage policy requires a contact)")
        if "youtube" in self.intelligence_connectors and not self.intelligence_youtube_api_key:
            raise ValueError("the youtube connector needs SAFFRON_INTELLIGENCE_YOUTUBE_API_KEY")
        if "court_records" in self.intelligence_connectors and not self.intelligence_indiankanoon_token:
            raise ValueError("the court_records connector needs SAFFRON_INTELLIGENCE_INDIANKANOON_TOKEN")
        if self.intelligence_map_enabled and self.environment == "production" and len(self.intelligence_suppression_key or "") < 32:
            raise ValueError("SAFFRON_INTELLIGENCE_SUPPRESSION_KEY (32+ characters) is required when the internet map is enabled in production")
        if self.environment == "production" and (self.intelligence_map_enabled or self.intelligence_search_provider != "disabled") and not self.intelligence_crawler_contact:
            # Without it every request says "+https://example.invalid/...": a site owner cannot reach whoever runs the crawler.
            raise ValueError("SAFFRON_INTELLIGENCE_CRAWLER_CONTACT (an https URL or an email address) is required in production when the crawler is on")
        if self.intelligence_crawler_contact:
            from ..internet_intelligence.fetch import crawler_user_agent

            try:
                crawler_user_agent(self.intelligence_crawler_contact)
            except ValueError as exc:
                raise ValueError("SAFFRON_INTELLIGENCE_CRAWLER_CONTACT must be an https URL or an email address") from exc
        if self.agent_planner not in {"deterministic", "model"}:
            raise ValueError("SAFFRON_AGENT_PLANNER must be deterministic or model")
        if self.agent_planner == "model" and self.model_provider == "deterministic":
            raise ValueError("SAFFRON_AGENT_PLANNER=model requires a real model provider")
        if self.approval_ttl_seconds <= 0:
            raise ValueError("SAFFRON_APPROVAL_TTL_SECONDS must be positive")
        if self.voice_agent_mode not in {"assistant", "agent"}:
            raise ValueError("SAFFRON_VOICE_AGENT_MODE must be assistant or agent")
        self._validate_voice()

    def _validate_voice(self) -> None:
        if not 60 <= self.voice_session_max_seconds <= 14_400:
            raise ValueError("SAFFRON_VOICE_SESSION_MAX_SECONDS must be between 60 and 14400")
        if not 15 <= self.voice_idle_timeout_seconds <= self.voice_session_max_seconds:
            raise ValueError("SAFFRON_VOICE_IDLE_TIMEOUT_SECONDS must be at least 15 and at most SAFFRON_VOICE_SESSION_MAX_SECONDS")
        if not 5 <= self.voice_ticket_ttl_seconds <= 300:
            raise ValueError("SAFFRON_VOICE_TICKET_TTL_SECONDS must be between 5 and 300")
        if not 1 <= self.voice_max_active_sessions <= 10_000:
            raise ValueError("SAFFRON_VOICE_MAX_ACTIVE_SESSIONS must be between 1 and 10000")
        if not 1 <= self.voice_max_sessions_per_person <= 20:
            raise ValueError("SAFFRON_VOICE_MAX_SESSIONS_PER_PERSON must be between 1 and 20")
        if not 1 <= self.voice_utterances_per_minute <= 120:
            raise ValueError("SAFFRON_VOICE_UTTERANCES_PER_MINUTE must be between 1 and 120")
        if self.voice_tts_provider not in {"browser", "polly"}:
            raise ValueError("SAFFRON_VOICE_TTS_PROVIDER must be browser or polly")
        if self.voice_tts_provider == "polly":
            if not self.voice_polly_region:
                raise ValueError("SAFFRON_VOICE_POLLY_REGION (or AWS_REGION) is required when SAFFRON_VOICE_TTS_PROVIDER=polly")
            if not re.fullmatch(r"[A-Za-z]{2,32}", self.voice_polly_voice_id):
                raise ValueError("SAFFRON_VOICE_POLLY_VOICE_ID must be a Polly voice name such as Kajal")
            if self.voice_polly_engine not in {"neural", "generative", "standard"}:
                raise ValueError("SAFFRON_VOICE_POLLY_ENGINE must be neural, generative, or standard")
        if not 0 < self.voice_tts_timeout_seconds <= 30:
            raise ValueError("SAFFRON_VOICE_TTS_TIMEOUT_SECONDS must be greater than 0 and at most 30")
        if not 1 <= self.voice_tts_max_concurrency <= 64:
            raise ValueError("SAFFRON_VOICE_TTS_MAX_CONCURRENCY must be between 1 and 64")
        if not 100 <= self.voice_tts_max_chars_per_reply <= 6000:
            raise ValueError("SAFFRON_VOICE_TTS_MAX_CHARS_PER_REPLY must be between 100 and 6000")
        if not 0 < self.conversation_timeout_seconds <= 60:
            raise ValueError("SAFFRON_CONVERSATION_TIMEOUT_SECONDS must be greater than 0 and at most 60")
        if not 0 < self.conversation_first_text_seconds <= self.conversation_stream_seconds <= 120:
            raise ValueError("SAFFRON_CONVERSATION_FIRST_TEXT_SECONDS must be positive and at most SAFFRON_CONVERSATION_STREAM_SECONDS, which is at most 120")
        if not 0 <= self.voice_filler_after_ms <= 10_000:
            raise ValueError("SAFFRON_VOICE_FILLER_AFTER_MS must be between 0 and 10000")
        if self.conversation_model_id and not re.fullmatch(r"[A-Za-z0-9._:/@-]{1,200}", self.conversation_model_id):
            raise ValueError("SAFFRON_CONVERSATION_MODEL_ID must be a model identifier")
        if not 0 <= self.web_searches_per_person_per_day <= 1000:
            raise ValueError("SAFFRON_WEB_SEARCHES_PER_PERSON_PER_DAY must be between 0 and 1000")
        for domain in self.assistant_web_exclude_domains:
            if any(character in domain for character in "/?#:") or urlparse(f"https://{domain}").hostname != domain:
                raise ValueError("SAFFRON_ASSISTANT_WEB_EXCLUDE_DOMAINS must contain hostnames, not URLs")


__all__ = ["AppSettings"]
