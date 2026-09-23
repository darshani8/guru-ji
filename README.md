# Guru Ji

Guru Ji is an AI-powered institutional intelligence platform for educational institutions. Institutions hand over the data they already keep (Excel, CSV, PDF, Word, scans, Google Sheets, existing databases); the platform ingests, maps, cleans, validates, deduplicates, and loads it into a canonical, tenant-isolated database, then lets authorised users perform institutional work through text or voice commands. A master agent plans, checks permissions, calls registered tools through a policy gateway, verifies, and reports. An internet intelligence agent adds source-backed public information about the institution. The original federated read-only connector path remains available alongside.

The implementation map for the platform is in [`docs/PLATFORM_BLUEPRINT.md`](docs/PLATFORM_BLUEPRINT.md).

## Platform quick start

    uv sync --extra dev
    make run
    # developers: open http://localhost:8000/console/ (demo role picker: student … institution_admin)
    # clients:    open http://localhost:8000/ (text and voice assistant)

Or from the command line against the running API:

    # register the institution (institution_admin), then upload a student sheet (principal/staff)
    curl -X PUT http://localhost:8000/v1/institutions/college_a -H 'Authorization: Bearer dev-token' -H 'X-Demo-Role: institution_admin' -H 'Content-Type: application/json' -d '{"name":"College A","location":"Bengaluru"}'
    curl -X POST http://localhost:8000/v1/ingestion/uploads -H 'Authorization: Bearer dev-token' -H 'X-Demo-Role: principal' -F file=@students.xlsx -F entity=student
    # then ask the agent to do work
    curl -X POST http://localhost:8000/v1/agent/commands -H 'Authorization: Bearer dev-token' -H 'X-Demo-Role: principal' -H 'Content-Type: application/json' -d '{"command":"Find all MBA students below 75% attendance and send the report to the HOD"}'

The client assistant (`apps/web/assistant/`, served at `/`) and the developer platform console (`apps/web/console/`, served at `/console/`) are separate apps that share only the sign-in client and base styles (`apps/web/shared/`); neither links to the other. The console is served outside production by default and is off in production unless `GURU_WEB_CONSOLE_ENABLED=true`, so a client-facing deployment serves only the assistant. With OIDC, register both `https://<host>/` and `https://<host>/console/` as callback and sign-out URLs, since each app returns to itself after sign-in.

`make platform-smoke` runs that sequence end to end. Local defaults need no external services (SQLite, local object store, inline job queue, hashing embeddings, deterministic planner, outbox email). See `.env.example` for PostgreSQL, S3, SQS, OCR, SMTP/SES, embeddings, model planner, and the Tavily-backed internet intelligence provider.

## Platform surface

- `POST /v1/ingestion/uploads`, `/v1/ingestion/sheets`, job inspection, mapping approval, duplicate review, commit, import reports.
- `GET /v1/data/*` unified, minimised data access (students, attendance, fees, exams, faculty, programs, events, admissions).
- `POST /v1/agent/commands` master agent (text or voice transcripts), `/v1/agent/tools`, approvals for high-risk actions, background jobs, run history.
- `POST /v1/documents` and `/v1/documents/search` document intelligence with page-level citations.
- `PUT /v1/intelligence/profile`, `POST /v1/intelligence/investigate`, mentions, digest, monitoring runs.
- `GET /v1/reports/{id}/download`, notifications, email outbox, institutions.

## Voice conversation

The assistant page holds a two-way spoken conversation. The microphone stays on while Guru Ji speaks, so the person can talk over a reply: it stops at once and the new question is answered. Saying "stop", "ruko", "bas" or "ನಿಲ್ಲಿಸು" only silences it. Phrases said with a short pause become one request, and the echo of Guru Ji's own voice is ignored. On iPhone and iPad, where the microphone and speaker cannot both run, it listens between replies and shows a Stop button.

- **Free conversation.** Greetings, general questions and follow-ups get a real reply. Anything the master agent cannot map becomes conversation with the configured model instead of "could not map this request". Institutional figures come only from the tools. The recent turns travel with each request; the server stores no transcript.
- **Internet by voice.** "Search the internet for …", "latest news on …", "weather in …" (also in Hindi and Kannada) run an open-web search through the Tavily settings, with cited sources on screen. Results are untrusted data, queries carrying personal data are refused, and each person has a daily allowance (`GURU_WEB_SEARCHES_PER_PERSON_PER_DAY`). Questions about the institution's own presence online still go to the internet-intelligence tools.
- **Fast replies.** A model reply is streamed: the first sentence is spoken while the model is still writing the rest, and the words appear on screen as they are said. A turn that takes a moment gets a short "one moment" (`GURU_VOICE_FILLER_AFTER_MS`). If a reply fails after it started speaking, it is withdrawn and a standard reply is spoken instead.
- **Indian voice and languages.** Replies come back in the language spoken: Indian English, Hindi (Devanagari or Hinglish) or Kannada. With `GURU_VOICE_TTS_PROVIDER=polly` they are spoken by Amazon Polly's Kajal (neural, en-IN and hi-IN), sentence by sentence. Otherwise, and for Kannada, the device's own Indian voice speaks.
- **Protocol.** `POST /v1/voice/sessions` returns a one-use ticket. The client opens `/v1/voice/sessions/{id}/stream`, authenticates with the ticket, and opts into `thinking`, `speech` and `interrupt`. It then sends `utterance` (text, language, history) and `interrupt`, and receives `thinking`, `speech` (sentence text, and Polly MP3 when configured; streamed replies send their first sentences before `answer`), `answer`, `speech_end` and `cancelled` (`retracted` when a streamed reply failed after speaking). A turn that reached the master agent is never cut short; it finishes and is shown without being spoken. Sessions are rows in the control database, so any API task can serve the socket.

## Current state

The repository now contains the complete all-phase reference implementation for the defined contracts:

- P0 security and contracts: deny-by-default principals, capabilities, College/Department/Batch scope, verified OIDC/JWKS boundary, consent/revocation safeguards, redaction, request limits, rate limiting, security headers, and read-only SQL safety.
- P1 institution boundary: an authenticated `apps/connector` service with health/readiness, approved semantic tools, server-to-server bearer authentication, scope/consent enforcement, effective-scope attestation, provenance, redaction markers, a local aggregate repository, and a PostgreSQL reporting-view adapter.
- P2 policy: a strict Cerbos HTTP decision-point adapter and versioned Cerbos sidecar policy/configuration under `infra/cerbos/`; local policy remains available for isolated tests.
- P3 control plane: SQLite and PostgreSQL stores for audit, source health, briefing history, answer-envelope hashes/counts, model attempts, retention pruning, and retry-safe outbox delivery.
- P4 evidence/streaming: provenance-aware results and closed, versioned SSE events with contiguous ordering (`message_start`, citation/warning, delta, answer, `message_end`, `done`), plus closed WebSocket voice input.
- P5 providers/operations: deterministic and Ollama adapters, optional LiteLLM streaming/usage adapter, redaction-safe trace export, provider capability declarations, and production configuration guards.
- P6 edge/integration boundaries: trusted Open edX/Moodle identity adapters, fail-closed OpenFGA checker, allowlisted MCP/agent-gateway targets, and LiveKit/Pipecat-style voice event normalization without raw-audio persistence.

The College A deterministic connector remains available only for development/tests. The API also supports a deployment-managed `GURU_INSTITUTION_CONNECTORS` JSON registry so one universal read-only contract can route to multiple college connector services; each entry supplies its own source ID, institution ID, endpoint, approved tools, and secret-environment reference. The legacy single-connector variables remain supported. The reference compose stack adds PostgreSQL, Cerbos, and the authenticated connector service; it does not create or contact a real college system. The browser voice path receives final transcripts only, rejects binary audio frames, uses one-use tickets shared through the control database, and keeps bounded session, idle and utterance limits.

The detailed ledger is in `docs/IMPLEMENTATION_STATUS.md`. The executable staging, production-canary, evidence, rollback, and sign-off sequence is in `docs/EXTERNAL_VALIDATION_PLAN.md`. These documents distinguish repository-complete implementation from production gates requiring real institutional contracts, credentials, deployments, and approvals.

## Validation

With Python 3.12+ and project dependencies synchronized:

    uv sync --extra dev
    uv run pytest -q
    make compile
    make validate-openapi
    make hygiene
    make lint

The full suite contains 200+ passing tests covering the connector path and the data platform. The CI-equivalent undefined-name/import lint check passes. `make smoke` validates a running API; `make postgres-smoke` validates a configured PostgreSQL service; `make platform-postgres-smoke` exercises the data platform stores, the intelligence store, and row-level security against a disposable PostgreSQL database; and `docker compose -f infra/docker/compose.dev.yml up --build` exercises the reference multi-service stack when Docker is available.

## Local configuration

Copy `.env.example` and keep `GURU_ENVIRONMENT=development` for the local deterministic path. The development identity uses `Authorization: Bearer dev-token` plus explicit demo headers. To exercise the remote connector path, disable `GURU_ENABLE_DEMO_DATA`, configure the connector URL/token, enable scope attestation, and use the service in `infra/docker/compose.dev.yml`. To exercise Cerbos, set `GURU_PDP_MODE=cerbos` and configure `GURU_CERBOS_URL`.

LiteLLM, public-web search, edge adapters, OpenFGA, trace export, MCP/agent gateway, and LiveKit/Pipecat are explicit adapters. They are not enabled by default and require deployment-managed endpoints, credentials, network policy, evaluation, and operational approval. Production configuration rejects the development identity, deterministic demo data, SQLite control-plane defaults, missing OIDC, missing connector attestation, non-TLS connector/Cerbos URLs, deterministic model selection, and non-fail-closed audit settings.

## Example request

    curl -X POST http://localhost:8000/v1/chat \
      -H 'Authorization: Bearer dev-token' \
      -H 'X-Demo-Principal: student-1' \
      -H 'X-Demo-Role: student' \
      -H 'X-Demo-College: college_a' \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"What is the current attendance summary?","institution_scope":{"college_id":"college_a"}}'

No production deployment, external institutional mutation, hosted-model request, or credentialed real integration is claimed by this build workflow.
