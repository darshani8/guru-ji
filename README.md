# Guru Ji

Guru Ji is a private BGS institutional AI assistant. It gives authorized users one ChatGPT-style interface for asking questions across approved Finance, College, and MATS sources while keeping those source databases separate and read-only.

The interface will support:

- Text chat with streamed responses.
- A focused, responsive conversation UI inspired by modern AI assistants, with an original Guru Ji visual identity.
- Browser voice conversations with interruption and playback support.
- Local voice mode as an optional privacy- and cost-conscious path.
- Current public-web research with citations.
- Institutional briefings such as “what is going on today?”.
- Explicit source status, freshness, partial-result warnings, and audit records.

## Product boundary

Guru Ji is a controlled read-only assistant, not an unrestricted autonomous agent. The model may propose a registered tool call, but application code decides whether the authenticated user is allowed to run it.

Institutional data access uses source-specific read-only connectors. The first implementation uses semantic tools such as `get_attendance_summary`, `get_daily_fee_collection`, and `get_source_health` instead of allowing a model to execute arbitrary SQL.

The application-owned control database stores users, scopes, source metadata, policy versions, conversations, usage, health, and audit events. It is not a replacement for the institutional databases.

## Technology direction

- Python and FastAPI for the versioned API.
- Pydantic for typed request, response, and tool contracts.
- SQLAlchemy and vendor drivers for heterogeneous read-only database connectors.
- PostgreSQL for control-plane data.
- A provider adapter for hosted and local language models.
- Ollama as an optional local model gateway.
- A search-provider adapter for public web research.
- Server-Sent Events for streamed text responses.
- WebRTC or a local WebSocket gateway for voice.
- OpenTelemetry and Sentry-compatible error monitoring.

## Interaction model

Chat and voice use the same internal execution core:

```
text or audio
    -> authenticated principal
    -> deterministic authorization
    -> typed tool plan
    -> approved read-only connectors/search
    -> provenance-aware results
    -> cited answer and audit event
```

Voice is a channel, not a second business-logic implementation. The voice session must use the same user scope, tools, redaction, limits, and audit rules as text chat.

## Security rules

1. No public database ports.
2. No write credentials for institutional sources.
3. No permanent model provider key in the browser.
4. No arbitrary model-generated SQL in the default path.
5. Authorization runs outside the model.
6. Webpages, database text, logs, and uploaded documents are untrusted data.
7. Secrets and sensitive records are excluded from prompts and logs.
8. Source failures and incomplete totals are visible to the user.
9. Future external write actions require explicit human confirmation.

## Guided build protocol

This repository is built incrementally. Each implementation step follows this sequence:

1. Select exactly one file.
2. Write or modify only that file.
3. Validate the file and explain its complete purpose, code, and related concepts.
4. Commit and push that file to `main`.
5. Stop and wait for the next instruction.

No second implementation file is started until the previous step is explained and accepted.

## Delivery phases

### Phase 1 — foundation

Create the API skeleton, settings, typed domain models, authentication boundary, policy boundary, control-database boundary, and health endpoint.

### Phase 2 — read-only tools

Add the source registry, connector interface, one institution connector, semantic tools, query limits, provenance, and audit events.

### Phase 3 — ChatGPT-style chat UI

Add the web client with conversation history, streamed text, source citations, warning states, responsive layout, keyboard navigation, and accessible loading/error states.

### Phase 4 — voice

Add voice-session creation, microphone permissions, WebRTC or local gateway transport, speech events, interruption handling, playback state, transcript display, and voice-specific telemetry.

### Phase 5 — web research and briefings

Add public search, extraction, citations, daily briefing workflows, source health, freshness, and partial-result reporting.

### Phase 6 — production hardening

Add institution-local connectors, mTLS, rate limits, quotas, secret management, backups, load tests, security tests, and operational runbooks.

## Repository documents

- `ARCHITECTURE.md` — system design and safety invariants.
- `FOLDER_STRUCTURE.md` — proposed implementation layout.
- `openapi.yaml` — versioned HTTP contract.

## Current status

The repository currently contains the architecture documents. The next code step is to create the minimal project scaffold without connecting to any institutional database or external model provider.
