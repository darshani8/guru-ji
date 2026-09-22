# BGS Federated Read-Only AI Agent

## Proposed repository structure

```text
bgs-federated-ai/
├── ARCHITECTURE.md
├── README.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── Makefile
├── .env.example
├── .gitignore
├── .dockerignore
│
├── apps/
│   ├── api/
│   │   └── app/
│   │       ├── __init__.py
│   │       ├── main.py
│   │       │
│   │       ├── api/
│   │       │   ├── __init__.py
│   │       │   ├── dependencies.py
│   │       │   ├── error_handlers.py
│   │       │   └── routes/
│   │       │       ├── __init__.py
│   │       │       ├── health.py
│   │       │       ├── chat.py
│   │       │       ├── voice.py
│   │       │       ├── sources.py
│   │       │       ├── briefings.py
│   │       │       └── audit.py
│   │       │
│   │       ├── auth/
│   │       │   ├── __init__.py
│   │       │   ├── jwt_verifier.py
│   │       │   ├── principal.py
│   │       │   ├── scopes.py
│   │       │   └── oidc.py
│   │       │
│   │       ├── config/
│   │       │   ├── __init__.py
│   │       │   ├── settings.py
│   │       │   ├── source_registry.py
│   │       │   ├── policy_registry.py
│   │       │   └── model_registry.py
│   │       │
│   │       ├── domain/
│   │       │   ├── __init__.py
│   │       │   ├── principals.py
│   │       │   ├── requests.py
│   │       │   ├── responses.py
│   │       │   ├── provenance.py
│   │       │   ├── source_health.py
│   │       │   ├── briefing.py
│   │       │   ├── audit.py
│   │       │   └── errors.py
│   │       │
│   │       ├── policy/
│   │       │   ├── __init__.py
│   │       │   ├── authorization.py
│   │       │   ├── institution_scope.py
│   │       │   ├── data_classification.py
│   │       │   ├── redaction.py
│   │       │   ├── rate_limits.py
│   │       │   └── query_limits.py
│   │       │
│   │       ├── orchestration/
│   │       │   ├── __init__.py
│   │       │   ├── execution_core.py
│   │       │   ├── request_classifier.py
│   │       │   ├── planner.py
│   │       │   ├── plan_validator.py
│   │       │   ├── tool_executor.py
│   │       │   ├── result_aggregator.py
│   │       │   ├── answer_synthesizer.py
│   │       │   ├── completeness.py
│   │       │   └── briefing_workflow.py
│   │       │
│   │       ├── tools/
│   │       │   ├── __init__.py
│   │       │   ├── registry.py
│   │       │   ├── base.py
│   │       │   ├── health_tools.py
│   │       │   ├── finance_tools.py
│   │       │   ├── college_tools.py
│   │       │   ├── mats_tools.py
│   │       │   └── briefing_tools.py
│   │       │
│   │       ├── connectors/
│   │       │   ├── __init__.py
│   │       │   ├── base.py
│   │       │   ├── registry.py
│   │       │   ├── execution_context.py
│   │       │   ├── common/
│   │       │   │   ├── sql_safety.py
│   │       │   │   ├── limits.py
│   │       │   │   ├── result_mapping.py
│   │       │   │   └── health.py
│   │       │   ├── finance/
│   │       │   │   ├── connector.py
│   │       │   │   ├── queries.py
│   │       │   │   ├── models.py
│   │       │   │   └── migrations_notes.md
│   │       │   ├── college_a/
│   │       │   │   ├── connector.py
│   │       │   │   ├── queries.py
│   │       │   │   └── models.py
│   │       │   ├── college_b/
│   │       │   │   ├── connector.py
│   │       │   │   ├── queries.py
│   │       │   │   └── models.py
│   │       │   ├── mats_a/
│   │       │   │   ├── connector.py
│   │       │   │   ├── queries.py
│   │       │   │   └── models.py
│   │       │   └── legacy_api/
│   │       │       ├── connector.py
│   │       │       ├── client.py
│   │       │       └── models.py
│   │       │
│   │       ├── providers/
│   │       │   ├── __init__.py
│   │       │   ├── model_base.py
│   │       │   ├── hosted_text.py
│   │       │   ├── ollama.py
│   │       │   ├── realtime_voice.py
│   │       │   ├── search_base.py
│   │       │   ├── tavily_search.py
│   │       │   └── embeddings.py
│   │       │
│   │       ├── voice/
│   │       │   ├── __init__.py
│   │       │   ├── session_manager.py
│   │       │   ├── ephemeral_credentials.py
│   │       │   ├── realtime_events.py
│   │       │   ├── local_gateway.py
│   │       │   ├── transcription.py
│   │       │   └── playback.py
│   │       │
│   │       ├── web_research/
│   │       │   ├── __init__.py
│   │       │   ├── search.py
│   │       │   ├── extraction.py
│   │       │   ├── untrusted_content.py
│   │       │   ├── citations.py
│   │       │   └── domain_allowlist.py
│   │       │
│   │       ├── persistence/
│   │       │   ├── __init__.py
│   │       │   ├── database.py
│   │       │   ├── repositories/
│   │       │   │   ├── principals.py
│   │       │   │   ├── sources.py
│   │       │   │   ├── conversations.py
│   │       │   │   ├── requests.py
│   │       │   │   ├── tools.py
│   │       │   │   ├── briefings.py
│   │       │   │   └── audit.py
│   │       │   └── models/
│   │       │       ├── principal.py
│   │       │       ├── source.py
│   │       │       ├── conversation.py
│   │       │       ├── request_run.py
│   │       │       ├── tool_run.py
│   │       │       ├── briefing.py
│   │       │       └── audit_event.py
│   │       │
│   │       ├── observability/
│   │       │   ├── __init__.py
│   │       │   ├── tracing.py
│   │       │   ├── metrics.py
│   │       │   ├── logging.py
│   │       │   └── redaction.py
│   │       │
│   │       └── middleware/
│   │           ├── request_id.py
│   │           ├── access_log.py
│   │           ├── timeout.py
│   │           ├── security_headers.py
│   │           └── rate_limit.py
│   │
│   └── connector-agent/
│       └── app/
│           ├── main.py
│           ├── tunnel.py
│           ├── local_policy.py
│           ├── source_health.py
│           ├── tool_dispatch.py
│           └── settings.py
│
├── contracts/
│   ├── openapi.yaml
│   ├── json-schema/
│   │   ├── tool-plan.schema.json
│   │   ├── tool-result.schema.json
│   │   └── provenance.schema.json
│   └── events/
│       ├── chat.events.md
│       └── voice.events.md
│
├── config/
│   ├── sources.example.yaml
│   ├── policies.example.yaml
│   ├── tools.example.yaml
│   └── domains.example.yaml
│
├── migrations/
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
│
├── database/
│   ├── control-schema.sql
│   ├── reporting-view-examples/
│   │   ├── finance.sql
│   │   ├── college.sql
│   │   └── mats.sql
│   └── read-only-role-examples/
│       ├── postgres.sql
│       ├── mysql.sql
│       ├── sqlserver.sql
│       └── oracle.sql
│
├── tests/
│   ├── unit/
│   │   ├── test_authorization.py
│   │   ├── test_plan_validation.py
│   │   ├── test_redaction.py
│   │   ├── test_tool_registry.py
│   │   └── test_partial_results.py
│   ├── integration/
│   │   ├── test_chat_api.py
│   │   ├── test_source_health.py
│   │   ├── test_connector_read_only.py
│   │   └── test_briefing.py
│   ├── security/
│   │   ├── test_prompt_injection.py
│   │   ├── test_sql_safety.py
│   │   ├── test_cross_institution_access.py
│   │   ├── test_secret_redaction.py
│   │   └── test_response_limits.py
│   ├── contract/
│   │   ├── test_openapi_schema.py
│   │   └── test_sse_events.py
│   └── fixtures/
│       ├── principals.py
│       ├── source_results.py
│       └── malicious_inputs.py
│
├── scripts/
│   ├── validate_openapi.py
│   ├── check_read_only_queries.py
│   ├── seed_dev_control_db.py
│   ├── run_source_health.py
│   └── estimate_usage.py
│
├── infra/
│   ├── docker/
│   │   ├── Dockerfile.api
│   │   ├── Dockerfile.connector
│   │   └── compose.dev.yml
│   ├── terraform/
│   │   ├── modules/
│   │   ├── environments/
│   │   │   ├── dev/
│   │   │   ├── staging/
│   │   │   └── production/
│   │   │   └── README.md
│   ├── kubernetes/
│   │   └── README.md
│   └── observability/
│       ├── otel-collector.yaml
│       ├── prometheus.yaml
│       └── grafana-dashboards/
│
├── docs/
│   ├── threat-model.md
│   ├── data-classification.md
│   ├── connector-onboarding.md
│   ├── tool-authoring.md
│   ├── voice-transport.md
│   ├── operations-runbook.md
│   ├── incident-response.md
│   └── decisions/
│       ├── ADR-001-read-only-connectors.md
│       ├── ADR-002-semantic-tools-not-arbitrary-sql.md
│       ├── ADR-003-separate-control-database.md
│       ├── ADR-004-provider-adapter.md
│       └── ADR-005-local-connector-topology.md
│
└── .github/
    └── workflows/
        ├── test.yml
        ├── lint.yml
        ├── openapi.yml
        └── security-scan.yml
```

## Ownership rules

| Directory | Responsibility | Must not contain |
|---|---|---|
| `api/` | HTTP/WebSocket transport | SQL, provider-specific business logic |
| `auth/` and `policy/` | Identity and authorization | LLM decisions, database queries |
| `orchestration/` | Workflow and result coordination | Source-specific table names |
| `tools/` | Approved semantic capabilities | Secrets, arbitrary SQL from users |
| `connectors/` | Source-specific access | Public routes, model prompts |
| `providers/` | Model/search integrations | Authorization decisions |
| `persistence/` | Control-plane storage | Full institutional data copies |
| `voice/` | Voice session lifecycle | A second authorization path |
| `web_research/` | Search and extraction | Privileged instructions from webpages |
| `infra/` | Deployment and infrastructure | Application secrets committed to Git |
| `contracts/` | Stable external schemas | Implementation-specific internals |

## First implementation slice

Do not create every connector directory at once. Start with:

```text
apps/api/app/
  main.py
  api/routes/chat.py
  auth/principal.py
  policy/authorization.py
  orchestration/execution_core.py
  tools/registry.py
  tools/health_tools.py
  tools/college_tools.py
  connectors/base.py
  connectors/college_a/connector.py
  providers/model_base.py
  providers/ollama.py
  persistence/database.py
  persistence/repositories/audit.py
  domain/
contracts/openapi.yaml
config/sources.example.yaml
```

Add each institution only after its source inventory, read-only credentials, reporting views, data classification, and authorization rules are approved.


## Institutional data platform additions

```
apps/api/app/
├── actions/               # report files (csv/xlsx/pdf), email senders, notifications
├── agents/                # master agent, planners, specialists, bindings, verification
├── data_access/           # tenant-enforced domain queries + field minimisation policy
├── documents/             # chunking, embeddings, RAG with citations
├── gateway/               # tool specs, registry, ToolGateway (authorise/validate/approve/audit)
├── ingestion/             # parsers (csv/xlsx/docx/pdf/image/json), OCR adapters, connectors, service
├── institution_data/      # canonical schema, store, record models
├── internet_intelligence/ # profiles, search, fetch, entity resolution, relevance, store, monitoring
├── normalization/         # canonical model, mapping engine, cleaning, validation, deduplication
├── platform_tools/        # registered tools per domain group
├── storage/               # object store backends
└── workers/               # job queue backends and handlers
apps/web/assistant/                   # client text/voice assistant, served at /
apps/web/console/                     # developer platform console, served at /console/
apps/web/shared/                      # sign-in client and base styles used by both
migrations/002_institution_data.sql   # canonical schema + RLS (generated from the canonical model)
scripts/run_worker.py, run_monitor.py, platform_smoke.py, export_openapi.py
docs/PLATFORM_BLUEPRINT.md            # blueprint -> implementation map
```
