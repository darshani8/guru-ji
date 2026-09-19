# Guru Ji local reference implementation

## Implemented

- Framework-independent principals, capabilities, and College/Department/Batch scope primitives.
- Request and response contracts, deny-by-default authorization, bounded plans, and explicit audit outcomes.
- Explicit source registry, semantic tool registry, data classification, redaction, query limits, and read-only SQL safety helpers.
- Deterministic in-memory College A connector with aggregate overview, attendance, and source-health tools. It never contacts a real institution.
- Request source IDs are honored and must belong to the requested College. Unknown, inactive, cross-College, or unregistered sources are refused before connector execution.
- Department and Batch scope is preserved through the executor. The current demo tools explicitly refuse narrower scopes because they only return College-level aggregates.
- Policy-aware orchestration with plan classification, sequential connector execution, citations, warnings, complete/partial/refused statuses, and DENIED audit outcomes for unauthorized requests.
- Development-only identity adapter using Authorization: Bearer dev-token plus demo headers. Production must replace this with OIDC/JWT validation.
- Voice session lifecycle with a ten-session cap and ephemeral audio-retention semantics; realtime provider credentials are deliberately disabled.
- Optional public-web boundary with domain allowlisting and prompt-injection warnings. Web content remains untrusted data.
- FastAPI routes for liveness/readiness, chat, daily briefings, sources, audit, and voice session lifecycle.
- Source metadata and audit routes require explicit capabilities.
- Request-ID propagation, safe logging helpers, a browser client under apps/web, and local Docker/Make commands.
- SQLite control-plane persistence for audit events, source-health snapshots, and briefing records, with idempotent initialization from migrations/001_control_plane.sql.
- Database initialization and repeatable live HTTP smoke validation scripts under scripts/.

## Validation

The exported branch was checked locally with the declared runtime dependencies installed: 40 tests pass, including the HTTP contract tests; Python compilation passes; and the live Uvicorn smoke test passes with LIVE_SMOKE_OK. The live check covered readiness, source metadata, chat, briefing creation, audit history, and the mounted frontend. The Codespace itself remains unavailable because its GitHub billing/free-usage limit was reached, so this validation was run against the exported branch snapshot instead.

## Intentionally not claimed

This is a complete local vertical slice, not a production deployment. It does not include a PostgreSQL runtime backend yet, a live institutional database, real OIDC/JWT keys, hosted-model credentials, WebRTC signaling, secret management, or production observability. The College A connector remains deterministic demo data. Those integrations need the actual institution contracts, security approvals, and deployment secrets rather than invented placeholders. PostgreSQL conversion remains deliberately deferred until the SQLite-backed build has been accepted.

## Local flow

1. Install the project dependencies in a Python 3.12 environment.
2. Run `make db-init` to create the local SQLite control database.
3. Run `make test` and `make compile`.
4. Run `make run`.
5. Run `make smoke` against the running API.
6. Send a development request with Authorization: Bearer dev-token, X-Demo-Principal, and X-Demo-Role.
7. Serve apps/web from the same origin or use the API routes directly.
