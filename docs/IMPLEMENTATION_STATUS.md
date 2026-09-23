# Guru Ji all-phase implementation status

## Repository implementation completed

All six roadmap phases now have code, contracts, tests, and deployment reference configuration in the review branch. The implementation remains application-owned at the identity, authorization, provenance, redaction, and read-only boundaries; external frameworks are adapters, not authorities.

### P0 — contracts and security

- Explicit principals, capabilities, and College/Department/Batch scopes.
- Deny-by-default authorization with revoked-principal and verified-consent checks.
- Development demo identity requires the exact configured bearer token; consent and revocation are never accepted from client headers.
- Production OIDC/JWKS verification maps only verified claims to roles, capabilities, consent state, revocation state, and scopes.
- Bounded requests, timeouts, rate limits, security headers, stable error envelopes, source/tool allowlists, redaction, and read-only SQL safety remain enabled.

### P1 — institutional connector

- Added an authenticated connector service under `apps/connector` with `GET /v1/health`, `GET /v1/ready`, and `POST /v1/execute`.
- The connector accepts only contract version 2 and approved semantic tools, rejects revoked or out-of-scope principals, requires `ask:read_only`, enforces student batch consent, and returns an attested effective scope plus provenance/redaction metadata.
- Added a local deterministic aggregate repository for stack tests and a PostgreSQL reporting-view adapter intended for a database role with SELECT-only privileges. No raw student records or writes cross the connector boundary.
- Added a deployment-managed multi-college connector registry with backward-compatible single-connector settings. Each registered source is bound to one institution, approved semantic tools, a distinct endpoint, a server-side secret reference, and optional scope attestation; plan construction selects only sources for the requested College scope.
- Added `infra/docker/Dockerfile.connector` and a development compose service.

### P2 — policy decision point

- Added a strict Cerbos HTTP adapter with fail-closed behavior for transport errors, malformed responses, stale/mismatched policy versions, unknown actions, revoked identities, missing capability, and over-broad matched scopes.
- Added versioned Cerbos configuration and a connector resource policy under `infra/cerbos/`.
- Local policy remains available for isolated tests; production configuration requires Cerbos over HTTPS.

### P3 — control plane and audit

- SQLite and PostgreSQL stores now share audit, source-health, briefing, answer-envelope, model-attempt, and outbox schemas.
- Answer envelopes store counts and a SHA-256 correlation hash, not answer text. Model attempts store provider/model, outcome, latency, fallback, usage, and cost fields without prompts or completions.
- Startup retention pruning is configured and bounded by `GURU_AUDIT_RETENTION_DAYS`.
- Outbox delivery is retry-safe: records are acknowledged only after the handler succeeds.
- The PostgreSQL smoke script covers migration, persistence, outbox delivery, and retention against a real configured database URL.

### P4 — evidence and streaming

- Source results preserve provenance, reporting period, redaction markers, citations, warnings, and partial/refused outcomes.
- SSE is a closed, versioned event union with contiguous sequence validation: `message_start`, citations, warnings, deltas, answer, `message_end`, and `done`.
- Existing browser/TestClient answer/done compatibility is retained while the richer event sequence is emitted.
- Voice WebSocket input remains a closed Pydantic contract with final-transcript-only handling and binary-frame rejection.

### P5 — providers and operations

- Deterministic and Ollama providers remain behind the model protocol.
- Added an optional LiteLLM adapter with normalized async streaming events and usage metadata; it is enabled only by explicit configuration and optional dependency installation.
- Added redaction-safe HTTP trace export for internal collectors. Export failures are best effort and never alter authorization or answer behavior.
- Added provider capability declarations and runtime configuration guards for production model selection.

### P6 — edge and full integration boundaries

- Added trusted Open edX and Moodle edge adapters. They call configured server endpoints and derive capabilities from server-returned roles; browser headers cannot self-elevate.
- Added an optional fail-closed OpenFGA relationship checker.
- Added an allowlisted MCP/agent-gateway target resolver; arbitrary client-supplied tool URLs are rejected.
- Added LiveKit/Pipecat-style event normalization that carries lifecycle/transcript metadata only and never persists raw audio.
- Existing browser voice transport remains the safe local reference path; provider-backed WebRTC/STT/TTS is intentionally an external deployment adapter.

### P7 — institutional data platform

- Ingestion of Excel, CSV, JSON, Word, PDF, images/scans (OCR adapters), Google Sheets, and existing databases into an intermediate representation with lineage.
- AI mapping engine with deterministic scoring, entity detection, human review for uncertain mappings, validated model suggestions, and reusable approved profiles.
- Safe cleaning, validation with OCR suspicion, deterministic-then-fuzzy deduplication with human review, and re-import reports (new / updated / unchanged / skipped).
- Canonical tenant-isolated database (SQLite locally, PostgreSQL with row-level security in deployment), object storage (memory/local/S3), and a background job queue (inline/thread/SQS).
- Unified data access service with role-based data minimisation, a tool/policy gateway with closed argument schemas and single-use approvals for high-risk actions, and 26 registered platform tools.
- Master agent with deterministic and model planners, data/action/internet specialists, step bindings, verification, audit, and background execution with notifications; voice can route transcripts through the agent.
- Document intelligence (chunking, embeddings, classification-aware retrieval, cited answers) and internet intelligence (profiles, query generation, robots-aware fetching, entity resolution, relevance/date filters, evidence store, monitoring digests and alerts).
- Routes, OpenAPI contract, Cerbos policy for platform tools, platform console page, worker and monitor scripts, compose services, and tests for every layer. See `docs/PLATFORM_BLUEPRINT.md`.

### P8 — internet map

- A graded register of each institution's official websites and accounts (`app/internet_intelligence/map/`): entities (including look-alikes), assets keyed so one account is one row whatever URL form it was found under, an append-only evidence log, and one versioned grading rule (O / A / A-arch / B / C / D) that recomputes every grade from evidence.
- Seed import from the 22 September 2026 manual sweep with a hidden holdout and look-alike canaries; metrics measure recall and leaks against them, and a rescore is published only if they do not get worse.
- A scheduled engine with per-institution locking, leases, work classes, daily budgets per institution and platform-wide, per-institution conditional requests, adaptive intervals, lead expiry and hop limits, yield-based priority and gap-targeted search.
- Connectors: official-site harvest, lead pages, re-checks and owner claims (always on; public pages and the institution's own domains only), and search, spam probe, feeds, YouTube Data API, Wikidata, IndianKanoon (review only), certificate transparency, RDAP, DNS over HTTPS, Wayback, link hubs and directories (each off until configured).
- A manager-only review queue, incidents with guidance and severity routing (immediate alerts and a daily digest), owner confirmation by token, suppression of personal accounts, reader restrictions on sensitive topics, a per-person investigation allowance, routes, a console card, and a guide for institution web administrators.
- A retention period for intelligence data (India's DPDP Act), `GURU_INTELLIGENCE_RETENTION_DAYS` (default 365, 30 to 3650), is applied at start-up and with each daily map digest. It deletes monitoring documents, events, runs and reports, and the map's closed review items, resolved incidents, runs, stale validators, abandoned sources and superseded observations. It blanks free-text evidence detail and keeps every row the grader reads, so no grade moves. Quota rows go after 30 days. Each institution is pruned in its own row-level-security tenant transaction.
- Every external service is exercised only through fake transports in the tests; no platform was logged in to and no real site or API was contacted by the test suite.

## Validation completed in this export

- `102 passed` in the full pytest suite, including the new all-phase tests.
- Python compilation passes for API, connector, tests, and scripts.
- Ruff CI-equivalent undefined-name/import check (`ruff check --select F`) passes.
- New targeted tests cover Cerbos allow/deny behavior, connector authentication/scope attestation, SQLite metadata/outbox retry behavior, trusted edge claims, MCP allowlisting, trace redaction, voice normalization, and stream sequencing.

The remaining repository validation commands are `make validate-openapi`, `make hygiene`, `make smoke` against a running API, `make postgres-smoke` against a real PostgreSQL service, and a compose-stack smoke test with Cerbos and the connector. They require the relevant local processes or external service to be available; their results are not invented here. The executable deployment-specific external validation sequence is documented in [`docs/EXTERNAL_VALIDATION_PLAN.md`](EXTERNAL_VALIDATION_PLAN.md).

## External gates still required before production claim

The repository implementation is complete for the defined roadmap contracts, but production readiness cannot honestly be marked 100% without these real resources:

- institution-approved connector views, schema, network path, service identity, and read-only PostgreSQL credentials;
- the production OIDC issuer, JWKS, claim mapping approval, revocation source, and consent source;
- a deployed/pinned Cerbos sidecar with policy load and TLS/mTLS validation;
- an approved LiteLLM/model gateway, model evaluation, cost limits, and server-side secret management;
- an internal trace collector, OpenFGA store/model, LMS edge endpoints, MCP/agent gateway, and LiveKit/Pipecat deployment if those adapters are selected;
- multi-worker deployment, connection-pool sizing, backup/restore, alerting, load testing, and incident/runbook validation.

No real college system, hosted model, identity provider, or production credential was contacted or claimed by this implementation pass.

## Local flow

1. Synchronize dependencies with `uv sync --extra dev`.
2. Run `uv run pytest -q` or `make test`.
3. Run `make compile`, `make validate-openapi`, `make hygiene`, and the CI-equivalent `make lint`.
4. Run `make db-init`, `make run`, and `make smoke` for the SQLite vertical slice.
5. Run `docker compose -f infra/docker/compose.dev.yml up --build` to exercise the reference PostgreSQL/Cerbos/connector stack when Docker is available.
6. Set deployment-managed production values only after the external gates above are approved.
