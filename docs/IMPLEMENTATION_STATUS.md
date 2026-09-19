# Guru Ji local reference implementation

## Implemented

- Framework-independent principals, capabilities, and College/Department/Batch scope primitives.
- Request and response contracts, deny-by-default authorization, bounded plans, and explicit audit outcomes.
- Explicit source registry, semantic tool registry, data classification, redaction, query limits, and read-only SQL safety helpers.
- Deterministic in-memory College A connector with aggregate overview, attendance, and source-health tools. It never contacts a real institution.
- Institution-local `RemoteHttpConnector` contract with bounded HTTPS/HTTP transport, explicit tool allowlisting, request IDs, read-only semantic execution, health mapping, response-size limits, provenance validation, and safe unavailable/timeout outcomes. It is registered only when explicitly configured.
- Request source IDs are honored and must belong to the requested College. Unknown, inactive, cross-College, or unregistered sources are refused before connector execution.
- Department and Batch scope is preserved through the executor. The current demo tools explicitly refuse narrower scopes because they only return College-level aggregates.
- Policy-aware orchestration with plan classification, sequential connector execution, citations, warnings, complete/partial/refused statuses, and DENIED audit outcomes for unauthorized requests.
- Development-only identity adapter using Authorization: Bearer dev-token plus demo headers.
- JWKS-backed OIDC/JWT verification boundary for production, with issuer, audience, expiry, required-claim, and safe-algorithm checks; verified claims map only to explicit roles, capabilities, and institution scopes.
- Optional local Ollama wording provider selected only by explicit configuration. It receives the deterministic approved-source answer, preserves application-owned citations/status, rejects output that drops numeric facts, and falls back deterministically on provider failure.
- Voice session lifecycle with a ten-session cap and ephemeral audio-retention semantics; realtime provider credentials are deliberately disabled.
- Optional public-web research route (`POST /v1/research/web`) and explicit chat intent path (for example, `search the web`) using a provider-neutral contract and a Tavily-compatible HTTP adapter. It is disabled by default, requires `ask:read_only`, sends only bounded queries and configured official-domain allowlists, filters results again locally, follows no redirects, caps response bytes/time, strips non-visible HTML, returns retrieval timestamps and citations, and marks prompt-injection-like text as untrusted data with warnings. Ordinary institutional prompts never trigger web search implicitly.
- FastAPI routes for liveness/readiness, JSON chat, SSE chat (`stream=true` or `/v1/chat/stream`), daily briefings, sources, audit, voice session lifecycle, and explicit public-web research.
- Source metadata and audit routes require explicit capabilities.
- Request-ID propagation, safe logging helpers, bounded HTTP request-size, timeout, and in-process rate-limit middleware, security headers, stable HTTP/validation error envelopes, a browser client under apps/web, and local Docker/Make commands.
- Branch CI/security gates for unit tests, compilation, OpenAPI validation, repository hygiene, undefined-name linting, formatting of validation scripts, dependency auditing, and Dependabot updates.
- SQLite control-plane persistence for audit events, source-health snapshots, and briefing records, with idempotent initialization from migrations/001_control_plane.sql.
- PostgreSQL control-plane persistence with the same store interface and schema contract, idempotent initialization, conflict-safe writes, and explicit `postgresql://` / `postgres://` runtime selection.
- Production configuration guards that reject the development bearer token, missing CORS origins, SQLite production storage, deterministic demo data, missing connector configuration, invalid request-size limits, and unsafe model settings.
- Database initialization and repeatable live HTTP smoke validation scripts under scripts/.

## Validation

The exported branch snapshot was checked with Python 3.12 and the declared runtime dependencies installed. Eighty-three automated tests pass, including HTTP contract, JSON/SSE chat, persistence, OIDC/JWT verification, model-provider fallback, remote connector contracts, public-web provider/extraction/allowlist boundaries, explicit web-chat routing, configuration-hardening, request-size, timeout, rate-limit, security-header, and stable-error tests. Python compilation passes. OpenAPI validation, repository hygiene, undefined-name linting, and formatting gates pass. The live Uvicorn smoke test passes with `LIVE_SMOKE_OK` and covers readiness, source metadata, chat, briefing creation, audit history, and the mounted frontend. Public-web transport was validated with deterministic HTTP mocks; no live search-provider credential was used in this validation run. PostgreSQL adapter construction and configuration paths are covered, but no live PostgreSQL server was available in this validation run.

## Intentionally not claimed

This is a hardened local vertical slice, not a production deployment. It does not include a live institutional database, the institution's real connector service, the institution's real OIDC issuer/JWKS configuration, a production Ollama/model deployment, hosted-model credentials, WebRTC signaling, secret management, or production observability. The College A connector remains deterministic demo data unless a remote connector is explicitly configured. Those integrations need the actual institution contracts, security approvals, and deployment secrets rather than invented placeholders. PostgreSQL, OIDC/JWT verification, the remote connector contract, and the optional local model boundary now exist, but they still need private services, credentials, connection-pool tuning, identity-claim mapping approval, model evaluation, and deployment testing before production use.

## Local flow

1. Install the project dependencies in a Python 3.12 environment.
2. Copy `.env.example` to `.env`; use SQLite for local development.
3. Run `make db-init` to create the local SQLite control database.
4. Run `make ci` (or the individual `make test`, `make compile`, `make validate-openapi`, `make hygiene`, and `make lint` targets).
5. Run `make run`.
6. Run `make smoke` against the running API.
7. To exercise local model wording, set `GURU_MODEL_PROVIDER=ollama`, `GURU_OLLAMA_BASE_URL`, and `GURU_OLLAMA_MODEL_ID`; otherwise deterministic wording remains the default.
8. To opt into public-web research, set `GURU_WEB_SEARCH_PROVIDER=tavily`, `GURU_WEB_SEARCH_API_KEY`, and an explicit `GURU_WEB_ALLOWED_DOMAINS` list; the route remains disabled when the provider is `disabled`.
9. For deployment, set `GURU_ENVIRONMENT=production`, explicit approved `GURU_ALLOWED_ORIGINS`, a non-default identity configuration, and a private `CONTROL_DATABASE_URL` using PostgreSQL.
10. Connect real institutional systems only through reviewed read-only connector implementations.
