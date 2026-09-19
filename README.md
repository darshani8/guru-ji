# Guru Ji

Guru Ji is a federated, read-only institutional AI assistant reference implementation. It separates identity, policy, approved source connectors, semantic tools, bounded orchestration, citations, warnings, and audit metadata.

## Current state

This repository contains a complete local vertical slice rather than only architecture documents. Development uses deterministic College A demo data, while deployment can register a reviewed institution-local semantic connector over HTTPS. The application-owned control plane uses SQLite locally or PostgreSQL in deployment environments to persist audit metadata, source-health snapshots, and briefing envelopes. The SQLite migration in `migrations/001_control_plane.sql` and the PostgreSQL adapter use the same control-plane schema contract and initialize idempotently. An opt-in local Ollama provider can rewrite already-approved answers; the application preserves provenance and falls back to deterministic wording when the provider is unavailable or changes numeric facts.

Included: explicit principals and College/Department/Batch scope, deny-by-default authorization, source and tool registries, data classification and redaction, query limits, a read-only connector boundary, bounded text orchestration, citations and partial/refused outcomes, explicit DENIED audit outcomes, development identity headers, durable local control-plane storage, a PostgreSQL deployment adapter, request-size/timeout/rate-limit guards, security headers, stable HTTP error envelopes, bounded voice sessions with one-use WebSocket tickets, browser microphone permission and Web Speech recognition/TTS, final-transcript-only voice orchestration, an opt-in public-web research route and chat path, a small browser client, CI/security workflows, tests, and local Docker/Make files.

The completed local hardening pass adds explicit bearer enforcement for development fixtures, separate briefing capabilities and scope filtering, a Cerbos-shaped local PDP with versioned decision IDs, server-derived principal/scope propagation into remote semantic connectors, closed Pydantic SSE and WebSocket event contracts, capability-aware provider metadata, append-only structured audit metadata, redaction-safe trace spans, Docker dependency parity, and live-route OpenAPI parity validation. These are Guru Ji-owned boundaries; no external framework is granted authority over identity, policy, provenance, or read-only enforcement.

Public-web research is an explicit `POST /v1/research/web` operation or an explicit chat phrase such as `search the web`. It is disabled by default and requires `ask:read_only`. When enabled with `GURU_WEB_SEARCH_PROVIDER=tavily`, the adapter sends only the bounded query, result limit, and configured official-domain allowlist to the provider. Page retrieval is separate, follows no redirects, enforces response/time limits, strips non-visible HTML, returns citations and retrieval timestamps, and marks all fetched text as untrusted data. Provider credentials and raw prompts are not persisted in audit records. Ordinary institutional chat never silently expands into web search.

The current demo tools expose College-level aggregates only. Department- and Batch-scoped requests are deliberately refused until a connector declares narrower-scope support. Explicit source IDs are validated against the requested College before execution.

The implementation status document in `docs/IMPLEMENTATION_STATUS.md` records what is intentionally not enabled. The repository includes a JWKS-backed OIDC/JWT verification boundary, an institution-local semantic connector contract (`GET /v1/health` and `POST /v1/execute`), and an opt-in local Ollama wording adapter. It still needs the institution's real connector service, issuer configuration, model deployment, and deployment secrets. Live college databases, hosted-model credentials, WebRTC signaling, secrets management, and production observability require real contracts and approvals; this repository does not invent them. Production configuration rejects the development identity, deterministic demo data, and SQLite control-plane defaults.

## Validate locally

Use Python 3.12 or newer within the supported range.

    make ci
    make test
    make compile
    make validate-openapi
    make hygiene

With dependencies installed, run the API with:

    make run

The local development identity uses `Authorization: Bearer dev-token` and headers `X-Demo-Principal`, `X-Demo-Role`, and `X-Demo-College`. The mounted browser client is available from the API root. Request boundaries can be tuned with `GURU_MAX_REQUEST_BYTES`, `GURU_REQUEST_TIMEOUT_SECONDS`, `GURU_RATE_LIMIT_REQUESTS`, and `GURU_RATE_LIMIT_WINDOW_SECONDS`.

The browser Voice Assistant requests microphone permission from the button action, uses the browser's `SpeechRecognition`/`speechSynthesis` implementation, and sends only final text transcripts through an authenticated same-origin WebSocket. Guru Ji does not receive or persist raw microphone audio in this MVP; browser speech providers may have their own processing and retention terms. The voice session remains bounded by the five-minute lifetime and ten-active-session cap, and the WebSocket ticket is single-use and never placed in the URL.

## Example request

    curl -X POST http://localhost:8000/v1/chat \
      -H 'Authorization: Bearer dev-token' \
      -H 'X-Demo-Principal: student-1' \
      -H 'X-Demo-Role: student' \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"What is the current attendance summary?","institution_scope":{"college_id":"college_a"}}'

No production deployment or external institutional mutation is performed by this build workflow.
