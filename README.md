# Guru Ji

Guru Ji is a federated, read-only institutional AI assistant reference implementation. It separates verified identity, policy, approved source connectors, semantic tools, bounded orchestration, evidence, streaming, and control-plane metadata.

## Current state

The repository now contains the complete all-phase reference implementation for the defined contracts:

- P0 security and contracts: deny-by-default principals, capabilities, College/Department/Batch scope, verified OIDC/JWKS boundary, consent/revocation safeguards, redaction, request limits, rate limiting, security headers, and read-only SQL safety.
- P1 institution boundary: an authenticated `apps/connector` service with health/readiness, approved semantic tools, server-to-server bearer authentication, scope/consent enforcement, effective-scope attestation, provenance, redaction markers, a local aggregate repository, and a PostgreSQL reporting-view adapter.
- P2 policy: a strict Cerbos HTTP decision-point adapter and versioned Cerbos sidecar policy/configuration under `infra/cerbos/`; local policy remains available for isolated tests.
- P3 control plane: SQLite and PostgreSQL stores for audit, source health, briefing history, answer-envelope hashes/counts, model attempts, retention pruning, and retry-safe outbox delivery.
- P4 evidence/streaming: provenance-aware results and closed, versioned SSE events with contiguous ordering (`message_start`, citation/warning, delta, answer, `message_end`, `done`), plus closed WebSocket voice input.
- P5 providers/operations: deterministic and Ollama adapters, optional LiteLLM streaming/usage adapter, redaction-safe trace export, provider capability declarations, and production configuration guards.
- P6 edge/integration boundaries: trusted Open edX/Moodle identity adapters, fail-closed OpenFGA checker, allowlisted MCP/agent-gateway targets, and LiveKit/Pipecat-style voice event normalization without raw-audio persistence.

The College A deterministic connector remains available only for development/tests. The reference compose stack adds PostgreSQL, Cerbos, and the authenticated connector service; it does not create or contact a real college system. The browser voice path receives final transcripts only, rejects binary audio frames, uses one-use tickets, and keeps bounded session/utterance limits.

The detailed ledger is in `docs/IMPLEMENTATION_STATUS.md`. It distinguishes repository-complete implementation from production gates requiring real institutional contracts, credentials, deployments, and approvals.

## Validation

With Python 3.12+ and project dependencies synchronized:

    uv sync --extra dev
    uv run pytest -q
    make compile
    make validate-openapi
    make hygiene
    make lint

The current export’s full suite contains 102 passing tests. The CI-equivalent undefined-name/import lint check passes. `make smoke` validates a running API; `make postgres-smoke` validates a configured PostgreSQL service; and `docker compose -f infra/docker/compose.dev.yml up --build` exercises the reference multi-service stack when Docker is available.

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
