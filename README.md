# Guru Ji

Guru Ji is a federated, read-only institutional AI assistant reference implementation. It separates identity, policy, approved source connectors, semantic tools, bounded orchestration, citations, warnings, and audit metadata.

## Current state

This repository contains a complete local vertical slice rather than only architecture documents. Development uses deterministic College A demo data, while deployment can register a reviewed institution-local semantic connector over HTTPS. The application-owned control plane uses SQLite locally or PostgreSQL in deployment environments to persist audit metadata, source-health snapshots, and briefing envelopes. The SQLite migration in `migrations/001_control_plane.sql` and the PostgreSQL adapter use the same control-plane schema contract and initialize idempotently. An opt-in local Ollama provider can rewrite already-approved answers; the application preserves provenance and falls back to deterministic wording when the provider is unavailable or changes numeric facts.

Included: explicit principals and College/Department/Batch scope, deny-by-default authorization, source and tool registries, data classification and redaction, query limits, a read-only connector boundary, bounded text orchestration, citations and partial/refused outcomes, explicit DENIED audit outcomes, development identity headers, durable local control-plane storage, a PostgreSQL deployment adapter, a request-size guard, voice-session lifecycle, an opt-in public-web research route, FastAPI routes, a small browser client, tests, and local Docker/Make files.

Public-web research is an explicit `POST /v1/research/web` operation. It is disabled by default and requires `ask:read_only`. When enabled with `GURU_WEB_SEARCH_PROVIDER=tavily`, the adapter sends only the bounded query, result limit, and configured official-domain allowlist to the provider. Page retrieval is separate, follows no redirects, enforces response/time limits, strips non-visible HTML, returns citations and retrieval timestamps, and marks all fetched text as untrusted data. Provider credentials and raw prompts are not persisted in audit records. This route does not silently turn arbitrary chat prompts or search-page instructions into application actions.

The current demo tools expose College-level aggregates only. Department- and Batch-scoped requests are deliberately refused until a connector declares narrower-scope support. Explicit source IDs are validated against the requested College before execution.

The implementation status document in docs/IMPLEMENTATION_STATUS.md records what is intentionally not enabled. The repository includes a JWKS-backed OIDC/JWT verification boundary, an institution-local semantic connector contract (`GET /v1/health` and `POST /v1/execute`), and an opt-in local Ollama wording adapter. It still needs the institution's real connector service, issuer configuration, model deployment, and deployment secrets. Live college databases, hosted-model credentials, WebRTC signaling, secrets management, and production observability require real contracts and approvals; this repository does not invent them. Production configuration rejects the development identity, deterministic demo data, and SQLite control-plane defaults.

## Validate locally

Use Python 3.12 or newer within the supported range.

    make test
    make compile

With dependencies installed, run the API with:

    make run

The local development identity uses Authorization: Bearer dev-token and headers X-Demo-Principal, X-Demo-Role, and X-Demo-College. The mounted browser client is available from the API root.

## Example request

    curl -X POST http://localhost:8000/v1/chat \
      -H 'Authorization: Bearer dev-token' \
      -H 'X-Demo-Principal: student-1' \
      -H 'X-Demo-Role: student' \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"What is the current attendance summary?","institution_scope":{"college_id":"college_a"}}'

No push or remote deployment is performed by this build workflow.
