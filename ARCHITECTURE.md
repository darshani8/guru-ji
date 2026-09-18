# BGS Federated Read-Only AI Agent

**Document:** Production architecture
**Version:** 0.1
**Status:** Proposed design for implementation review
**Date:** 2026-09-18
**Scope:** Finance, multiple college databases, MATS databases, public web research, chat, voice, and institutional briefings

## 1. Executive decision

Build one application boundary, not one centralized data store:

> A modular Python/FastAPI application exposes one stable API while institution-specific read-only connectors remain responsible for their own databases, schemas, SQL dialects, data policies, and network boundaries.

The application provides:

- One API for React, Angular, PHP, .NET, Java, mobile, and legacy adapters.
- One authorization layer outside the language model.
- Semantic read-only tools instead of unrestricted LLM-generated SQL.
- Separate connectors for Finance, Colleges, MATS, and future sources.
- A public-web search adapter that treats web content as untrusted data.
- Shared chat and voice execution logic.
- A separate application-owned PostgreSQL control database for identity mappings, source registry, policy, conversations, audit, usage, and health state.
- Optional institution-local connector services when databases cannot safely be reached from the central application.

The architecture deliberately does **not** centralize institutional databases. It centralizes only the application control plane and user experience.

## 2. Goals

1. Answer authorized questions using multiple heterogeneous institutional data sources.
2. Preserve institutional database ownership and avoid a mandatory data migration.
3. Make every database path strictly read-only from the AI application's perspective.
4. Keep authorization, source selection, row filtering, and tool execution outside the model.
5. Support current public web information with citations and freshness metadata.
6. Support text chat, streaming chat, browser voice, and local voice as interchangeable channels.
7. Produce a useful "what is going on" institutional briefing from approved metrics.
8. Make failures explicit: unavailable sources must never silently appear empty or current.
9. Add a new database by implementing a connector and registering approved tools, without changing client applications.
10. Provide auditability, cost control, observability, and a path from a two-to-four-user pilot to a larger deployment.

## 3. Non-goals

This first version will not:

- Write to Finance, College, or MATS databases.
- Let the model execute arbitrary SQL by default.
- Expose database ports to the public internet.
- Promise complete knowledge of the internet.
- Automatically change institutional records, approve students, send messages, purchase anything, or take other consequential actions.
- Replace institutional applications or centralize all operational data.
- Start with Kubernetes, a large microservice fleet, or a multi-agent swarm.
- Treat a prompt filter as a complete security boundary.

## 4. Architecture invariants

These rules are mandatory and must be tested in code and deployment configuration.

### INV-01: The model is not an authority

The model may propose a tool call. It may not decide whether the caller is allowed to execute that tool. The policy engine authorizes every tool call using the authenticated principal, institution scope, source classification, requested operation, and applicable data policy.

### INV-02: Institutional data access is read-only

Each institutional connection uses a dedicated account with only the minimum required read permissions. Prefer approved read-only views or stored procedures over broad table access. The application must additionally reject non-read operations before a query reaches a connector.

### INV-03: No direct public database access

Institutional databases are reachable only through private networking, a private connector, or an institution-local connector service. No database listener is exposed to the public internet.

### INV-04: Semantic tools are the default

The model receives tools such as `get_attendance_summary` and `get_daily_fee_collection`, not unrestricted connection strings or unconstrained SQL execution.

### INV-05: Web content is untrusted

Search results, webpage text, documents, logs, and database text are data. They are never instructions. They cannot change system policy, permissions, tool definitions, destinations, or secrets.

### INV-06: Provenance is part of the answer

Every factual result carries source, retrieval time, time range, and completeness information. The response distinguishes exact source data from model interpretation.

### INV-07: Partial failure is visible

If one source fails, times out, returns an authorization denial, or is stale, the final response explicitly reports that condition and identifies which totals exclude it.

### INV-08: Secrets never enter prompts or logs

Database credentials, model API keys, ephemeral voice credentials, signing keys, and private connection details are held by server-side secret management only.

### INV-09: The control database is separate

The application-owned PostgreSQL database stores control and audit data. It is not a replacement for institutional systems and must not become an accidental copy of all institutional records.

### INV-10: One channel-independent execution core

Chat, browser voice, local voice, scheduled briefings, and future clients use the same authorization, orchestration, tool, provenance, and audit pipeline.

## 5. High-level architecture

```text
+-----------------------------------------------------------------------+
|                         CLIENT APPLICATIONS                           |
| React | Angular | PHP | .NET | Java | Mobile | Legacy adapter       |
+-----------------------------------+-----------------------------------+
                                    | HTTPS JSON / SSE / WebRTC setup
                                    v
+-----------------------------------------------------------------------+
|                       SECURITY EDGE / GATEWAY                         |
| TLS | OIDC/JWT | rate limiting | CORS | request limits               |
+-----------------------------------+-----------------------------------+
                                    v
+-----------------------------------------------------------------------+
|                       FASTAPI APPLICATION                            |
| Authentication -> Authorization -> Orchestrator -> Tool policy       |
|       |                 |                  |                          |
|       |                 |                  +--> semantic tools          |
|       |                 +--> institution/scope enforcement              |
|       +--> principal, roles, data classification                       |
|                                                                       |
| Chat API | voice-session API | briefing API | source-health API       |
+----------------------+--------------------------+---------------------+
                       |                          |
                       v                          v
          +-------------------------+   +----------------------------+
          | Connector runtime       |   | Web research adapter        |
          | SQLAlchemy/native/API   |   | Search -> extract -> cite   |
          | read-only execution     |   | untrusted-content boundary |
          +------------+------------+   +----------------------------+
                       |
                       v
+-----------------------------------------------------------------------+
|                    INSTITUTIONAL SOURCES                              |
| Finance DB | College A | College B | MATS 1 | MATS 2 | legacy APIs    |
+-----------------------------------------------------------------------+

Separate control plane:

+---------------------+  +-----------------+  +-------------------------+
| Control PostgreSQL  |  | Redis optional  |  | OpenTelemetry/Sentry    |
| users, policies,    |  | sessions,       |  | traces, metrics, logs,  |
| sources, audit      |  | rate limits     |  | error monitoring        |
+---------------------+  +-----------------+  +-------------------------+
```

## 6. Deployment topologies

### 6.1 Pilot topology: one private deployment

Use this first when the databases are reachable over a private network.

```text
Private VM or private container host
  +-- FastAPI application
  +-- connector runtime
  +-- control PostgreSQL
  +-- optional Redis

Private network
  +-- Finance database
  +-- College databases
  +-- MATS databases
```

This is the simplest deployment for two to four users. It is appropriate only when network access, credentials, latency, and institutional policy are acceptable.

### 6.2 Distributed topology: institution-local connectors

Use this when databases are on separate campus networks or must never accept inbound connections from a central service.

```text
                 Central private application
              +-------------------------------+
              | FastAPI + policy + orchestration|
              +---------------+---------------+
                              |
                         mTLS/outbound
      +-----------------------+------------------------+
      |                       |                        |
      v                       v                        v
+-------------+       +-------------+          +-------------+
| College A   |       | Finance     |          | MATS        |
| connector   |       | connector   |          | connector   |
| local       |       | local       |          | local       |
+------+------+       +------+------+          +------+------+
       |                     |                        |
       v                     v                        v
 College A DB          Finance DB                 MATS DB
```

A local connector:

- Initiates an outbound authenticated connection to the central application.
- Has local network access only to its institution's approved sources.
- Exposes only registered semantic tools.
- Does not expose the database itself.
- Returns bounded, typed results and provenance.
- Does not receive executable instructions from source data.

### 6.3 Choice

Start with 6.1 if all sources are in one controlled private network. Move to 6.2 for institutions that are isolated, have different owners, or require local control. The public API and client contract remain unchanged.

## 7. Component responsibilities

### 7.1 API edge

Responsibilities:

- HTTPS/TLS.
- Request size and timeout limits.
- Authentication token validation or forwarding to the application.
- CORS allowlist for known browser origins.
- Rate limits by principal, institution, IP, and endpoint.
- Correlation/request ID.
- Protection against accidental public exposure of internal health and audit routes.

CORS is not authentication. A valid CORS configuration does not protect an endpoint from non-browser callers.

### 7.2 FastAPI API layer

Responsibilities:

- Validate request and response schemas with Pydantic.
- Expose the versioned `/v1` contract.
- Stream chat output through SSE where requested.
- Create and close voice sessions.
- Enforce endpoint scopes.
- Convert internal typed errors to stable public error responses.
- Never contain source-specific SQL in route functions.

### 7.3 Authentication and principal context

The API accepts an institution-issued OIDC/JWT access token in production. The token is converted into:

```text
PrincipalContext
  subject_id
  display_name
  roles
  institution_ids
  department_ids
  data_scopes
  allowed_channels
  token_issued_at
  token_expiry
  correlation_id
```

For the pilot, a development bearer token may be used, but it must be clearly disabled in production.

### 7.4 Authorization and policy engine

The policy engine is deterministic application code. It checks:

- Is the identity authenticated?
- Is the user allowed to use this endpoint?
- Is the requested institution in the user's scope?
- Is the requested source active?
- Is the tool approved for that source?
- Is the data classification permitted?
- Is the requested time range allowed?
- Is the user allowed to receive row-level or aggregate data?
- Is the request within rate, row, cost, and execution limits?

The model never sees internal policy secrets. It receives a typed denial when a tool is not authorized.

### 7.5 Orchestrator

Use a deterministic workflow with constrained model steps:

```text
receive request
  -> normalize text
  -> classify intent
  -> determine candidate sources
  -> ask for clarification if scope is ambiguous
  -> produce typed tool plan
  -> validate plan
  -> authorize each tool call
  -> execute tools in parallel where safe
  -> collect provenance and completeness
  -> synthesize answer
  -> validate answer and citations
  -> audit
  -> return/stream result
```

The model may help classify intent or select among registered tools. The application validates its output against a closed schema. Unknown tool names, unknown source IDs, out-of-scope institutions, excessive limits, or malformed arguments are rejected.

Use LangGraph only when durable multi-step execution, resumability, human approval, or long-running workflows are needed. A typed Python workflow is preferable for the initial read-only query path because it is easier to test and audit.

### 7.6 Semantic tool registry

Every tool has:

```text
tool_name
version
purpose
allowed_sources
required_scopes
input_schema
output_schema
data_classification
max_rows
max_duration_ms
supports_parallel
redaction_policy
provenance_requirements
```

Recommended first tools:

- `get_source_health`
- `get_daily_fee_collection`
- `get_admission_activity`
- `get_attendance_summary`
- `get_pending_approvals`
- `get_latest_system_incidents`
- `get_institution_overview`

Do not expose student-level records until identity scope, masking, audit, and purpose restrictions are implemented.

### 7.7 Connector layer

A connector translates a common semantic tool into a source-specific operation:

```python
class ReadOnlyConnector(Protocol):
    source_id: str

    async def health(self) -> SourceHealth:
        ...

    async def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ExecutionContext,
    ) -> ToolResult:
        ...
```

Connector rules:

- Never accept a raw connection string from a user or model.
- Never construct SQL by concatenating user input.
- Use parameterized queries.
- Prefer approved database views/stored procedures.
- Enforce timeout and row limits at the connector.
- Return typed data, not arbitrary driver objects.
- Return source ID, retrieval timestamp, time range, and completeness.
- Redact or hash identifiers before the result leaves the connector when required.
- Keep source-specific schema knowledge inside the connector package.

SQLAlchemy is appropriate for common relational databases. Vendor-specific drivers or API clients are allowed for systems that do not fit SQLAlchemy.

### 7.8 Query safety

Semantic tools are the default. If a restricted analyst SQL tool is added later, it must enforce:

- `SELECT` only.
- Approved schemas/views only.
- One statement only.
- No comments or encoded bypasses.
- No DDL/DML, procedures with side effects, locks, or file access.
- Parameterized values.
- Maximum execution duration.
- Maximum returned rows and response bytes.
- Query cancellation on timeout.
- Query-plan/cost checks where available.
- Per-user quota and audit entry.

A read-only database credential is necessary but not sufficient. It does not prevent over-broad reads, expensive queries, or data leakage.

### 7.9 Model provider adapter

Use a provider-neutral interface:

```python
class ModelProvider(Protocol):
    async def plan(self, request: PlannerRequest) -> ToolPlan:
        ...

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        ...

    async def stream(self, request: SynthesisRequest) -> AsyncIterator[TokenEvent]:
        ...
```

The adapter should support hosted text models, hosted realtime voice, and a local Ollama model without changing API routes or connectors. The provider must never call institutional databases directly; tool execution stays in the application.

### 7.10 Web research adapter

```text
search(query, domains, date_range, depth)
  -> search results
extract(urls)
  -> page content
normalize and mark as untrusted
  -> research evidence
synthesize with citations
  -> answer
```

Rules:

- Web content is untrusted data.
- Do not place raw webpage text into a privileged system instruction.
- Do not expose institutional data to web search unless an explicitly authorized, redacted query is permitted by policy.
- Restrict domains for high-risk questions.
- Record URL, title, publication date when available, retrieval timestamp, and extraction status.
- Cite source claims in the final answer.
- Report when search results are stale, conflicting, incomplete, or unavailable.

### 7.11 Chat transport

- Non-streaming: `POST /v1/chat` returns JSON.
- Streaming: `POST /v1/chat?stream=true` returns `text/event-stream` events.
- Both use the same internal execution path.
- SSE is preferred for ordinary streamed text because it is simple for browser clients.
- WebSockets are appropriate for interactive bidirectional sessions or local voice transport.

### 7.12 Voice transport

#### Managed browser realtime mode

```text
Browser microphone/speaker
        | WebRTC
        v
Managed realtime voice provider
        | server-side tool calls/control
        v
FastAPI application
        | authorized semantic tools
        v
Institutional connectors / web adapter
```

The application server creates a short-lived client session credential. Permanent provider keys remain server-side. The voice model is not allowed to bypass the same authorization and tool registry used by chat.

#### Local voice mode

```text
Browser
  | WebSocket or WebRTC to local gateway
  v
FastAPI voice gateway
  +-- local speech recognition
  +-- local Ollama model
  +-- local TTS
```

Local voice reduces API cost and improves data locality, but quality and latency must be benchmarked on the available hardware.

### 7.13 Briefing engine

The "what is going on" capability is a scheduled or on-demand workflow over approved metrics, not an unrestricted scan:

```text
get_finance_activity_today()
get_admission_activity_today()
get_attendance_anomalies()
get_pending_approvals()
get_latest_mats_incidents()
get_source_health()
             |
             v
bounded parallel collection
             |
             v
completeness assessment
             |
             v
AI narrative synthesis with citations
```

A briefing reports the reporting period, sources included, sources unavailable or stale, exact counts and amounts, anomalies and thresholds, actions requiring human review, and the generated-at timestamp.

## 8. Data and control-plane design

### 8.1 Control PostgreSQL entities

Recommended tables:

```text
principals
principal_institution_scopes
roles
role_permissions
sources
source_capabilities
connector_health
conversations
conversation_messages
request_runs
tool_runs
citations
briefings
briefing_source_runs
audit_events
usage_events
policy_versions
redaction_events
```

Do not copy complete institutional tables into the control database. Store only the minimum identifiers and metadata needed for control, audit, traceability, and the user experience.

### 8.2 Suggested source record

```yaml
source_id: college_a_postgres
institution_id: college_a
connector_type: postgresql
network_mode: private
status: active
timezone: Asia/Kolkata
classification: confidential
allowed_tools:
  - get_admission_activity
  - get_attendance_summary
allowed_views:
  - reporting.student_admission_summary
  - reporting.attendance_daily_summary
limits:
  max_query_ms: 10000
  max_rows: 500
  max_concurrent_queries: 3
credentials_ref: secret://bgs/college-a/readonly
```

Credentials are referenced, not stored in this file or in source control.

### 8.3 Provenance record

```json
{
  "source_id": "college_a_postgres",
  "source_type": "internal_database",
  "retrieved_at": "2026-09-18T14:20:00+05:30",
  "data_period": {
    "from": "2026-09-18T00:00:00+05:30",
    "to": "2026-09-18T14:20:00+05:30"
  },
  "complete": true,
  "rows_used": 1,
  "redactions_applied": ["student_identifier"]
}
```

## 9. Authorization model

Use layered authorization:

```text
Endpoint authorization
  -> institution/scope authorization
  -> source authorization
  -> tool authorization
  -> row/field policy
  -> output redaction
```

Example permissions:

```text
chat.use
voice.use
briefing.read
briefing.run
source.health.read
finance.aggregate.read
college.aggregate.read
college.student_detail.read
mats.incident.read
audit.read
```

The initial deployment should expose aggregate reporting tools only. Student-level details, marks, attendance by person, financial personal data, and credentials require an explicit policy review.

## 10. Prompt-injection and data-exfiltration defenses

Threat sources include direct user prompts, webpages and search snippets, database text fields, uploaded documents, error logs, and any email or message content added later.

Controls:

1. Keep privileged instructions and untrusted content in separate message fields or clearly marked data blocks.
2. Use closed tool schemas and reject unknown tool names.
3. Authorize tools outside the model.
4. Apply least privilege to database and web-search scopes.
5. Never let retrieved content define a new tool or permission.
6. Use a quarantine/summarization stage for untrusted long documents when needed.
7. Validate model output against a schema.
8. Redact secrets and sensitive personal data before model or web transmission.
9. Add tests containing direct and indirect prompt-injection payloads.
10. Require human confirmation before any future external write action.

## 11. Failure and timeout semantics

Each source result must have one of these states:

```text
success
partial
stale
unauthorized
unavailable
timeout
invalid_result
```

The final answer should continue when safe, but must include warnings. Example:

```text
The briefing includes Finance and College A. College B timed out after 5 seconds, so the institutional total excludes College B.
```

Recommended initial limits:

```text
HTTP request timeout: 30 seconds
individual connector query timeout: 10 seconds
web search timeout: 8 seconds
maximum tool calls per request: 8
maximum parallel source calls: 5
maximum returned rows per tool: 500
maximum response size: 1 MB before pagination
```

Tune these through measured testing rather than treating them as permanent values.

## 12. Observability and audit

Use a correlation ID for every request and a trace/span hierarchy:

```text
request
  +-- authentication
  +-- planning
  +-- authorization
  +-- tool:college_a
  +-- tool:finance
  +-- web_search
  +-- synthesis
  +-- response
```

Metrics:

- Request count and error rate.
- p50/p95/p99 latency by endpoint and source.
- Connector timeout rate.
- Tool authorization-denial rate.
- Partial-answer rate.
- Model input/output tokens and estimated cost.
- Web-search credits consumed.
- Voice session duration and interruption rate.
- Database query duration and row count.
- Source freshness and health.
- Prompt-injection detection events.

Audit event fields:

```text
request_id
principal_id
endpoint
conversation_id
requested_scope
selected_sources
selected_tools
authorization_decisions
model_provider/model_id
source_result_statuses
redactions
latency
usage
created_at
```

Never log full student records, secrets, raw audio, permanent tokens, or complete sensitive query results.

## 13. API contract

The canonical HTTP contract is `openapi.yaml` in this design folder.

Stable endpoints:

```text
GET    /v1/health/live
GET    /v1/health/ready
POST   /v1/chat
POST   /v1/voice/sessions
DELETE /v1/voice/sessions/{session_id}
GET    /v1/sources/health
GET    /v1/briefings/today
POST   /v1/briefings/run
GET    /v1/audit/events
```

Client applications do not need to know whether the source is PostgreSQL, Oracle, MySQL, SQL Server, a legacy API, or a local connector. They use the same contract.

### Error contract

All errors use a stable JSON shape:

```json
{
  "error": {
    "code": "source_timeout",
    "message": "One authorized source did not respond within the configured limit.",
    "request_id": "req_123",
    "retry_after_seconds": 5
  }
}
```

Do not expose SQL text, database hostnames, stack traces, credentials, or internal provider errors to clients.

## 14. Repository implementation structure

The proposed repository layout is documented separately in `FOLDER_STRUCTURE.md`.

Important separation rules:

- `api/` contains transport only.
- `orchestration/` contains workflow, not SQL.
- `tools/` contains approved semantic operations.
- `connectors/` contains source-specific access.
- `policy/` contains authorization and redaction.
- `providers/` contains model and search adapters.
- `domain/` contains typed models and business-neutral result types.
- `infra/` contains deployment and infrastructure configuration.

## 15. Delivery plan

### Phase 1 — secure vertical slice

Build one source, three tools, one endpoint, and full audit:

- Control PostgreSQL.
- OIDC/JWT or a controlled development identity.
- One Finance or College connector.
- `get_source_health`.
- `get_institution_overview`.
- One aggregate metric tool.
- `/v1/chat`.
- Authorization tests.
- Read-only database tests.
- Partial failure tests.

### Phase 2 — additional sources

Add Finance, Colleges, and MATS one at a time. For each source:

1. Inventory schema and classification.
2. Create reporting views.
3. Create read-only credentials.
4. Implement connector.
5. Register tools.
6. Add authorization tests.
7. Validate result and provenance.
8. Add source health monitoring.

### Phase 3 — web and briefings

Add web search, citations, daily briefing tools, scheduled execution, freshness rules, and partial-result reporting.

### Phase 4 — voice

Add browser WebRTC or local voice after the text tool path is stable. Voice must call the existing execution core and must not introduce a separate authorization path.

### Phase 5 — scale and hardening

Add institution-local connectors, mTLS, private deployment, load testing, backup/restore, provider fallback, OpenTelemetry, and formal security review.

## 16. Test strategy

### Unit tests

- Tool argument validation.
- Policy decisions.
- Redaction.
- Source selection.
- Partial-result synthesis.
- Prompt-injection handling.
- Provider adapter behavior.

### Connector integration tests

- Read-only account cannot write.
- Query timeout is enforced.
- Row limit is enforced.
- Database outage returns `unavailable`.
- Invalid source result is rejected.
- Source timezone is applied correctly.

### API contract tests

- OpenAPI schema validation.
- Authentication failures.
- Scope failures.
- Stable error shape.
- SSE event ordering.
- Voice session expiry.

### Security tests

- Direct prompt injection.
- Indirect webpage injection.
- Tool-name injection.
- SQL keyword and multi-statement attempts.
- Cross-institution access attempts.
- Data exfiltration through web search.
- Excessive query and response sizes.
- Secret leakage in logs.

### Operational tests

- One source down.
- One source slow.
- Model provider unavailable.
- Search provider unavailable.
- Control database unavailable.
- Connector reconnect.
- Backup restore.

## 17. Decisions to confirm before production

1. Which database engines and versions are actually used by Finance, Colleges, and MATS?
2. Will the central service be allowed private network access, or are local connectors mandatory?
3. Which identity provider supplies institutional login?
4. Which users may see aggregate versus person-level information?
5. Which data may be sent to hosted model providers?
6. Is browser realtime voice required, or is push-to-talk sufficient for the first release?
7. Which public websites are allowed for web research?
8. What are the retention periods for conversations, audit events, and audio?
9. Which daily briefing recipients and delivery channels are approved?
10. What is the maximum concurrency and latency target?

## 18. Final implementation rule

Build one controlled read-only application, not an unconstrained autonomous agent:

```text
authenticated user
  -> deterministic policy
  -> typed plan
  -> authorized semantic tools
  -> read-only connectors
  -> provenance-aware results
  -> cited synthesis
```

This gives BGS one convenient AI interface without forcing every institution to surrender ownership of its database or requiring every existing application to adopt a new technology stack.
