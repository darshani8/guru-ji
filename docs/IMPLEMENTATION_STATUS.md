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

## Validation

The framework-independent suite currently passes 39 tests. Three conditional HTTP contract tests are skipped in the Codespace because FastAPI is not installed there; they will run automatically once the declared dependencies are installed. Python compilation and git diff whitespace validation pass.

## Intentionally not claimed

This is a complete local vertical slice, not a production deployment. It does not include a live institutional database, real OIDC/JWT keys, hosted-model credentials, WebRTC signaling, persistent control-plane migrations, secret management, or production observability. Those integrations need the actual institution contracts, security approvals, and deployment secrets rather than invented placeholders.

## Local flow

1. Install the project dependencies in a Python 3.12 environment.
2. Run make test and make compile.
3. Run make run.
4. Send a development request with Authorization: Bearer dev-token, X-Demo-Principal, and X-Demo-Role.
5. Serve apps/web from the same origin or use the API routes directly.
