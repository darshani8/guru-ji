# Agent Saffron

Agent Saffron is an AI-powered institutional intelligence platform for educational institutions. Institutions hand over the data they already keep (Excel, CSV, PDF, Word, scans, Google Sheets, existing databases); the platform ingests, maps, cleans, validates, deduplicates, and loads it into a canonical, tenant-isolated database, then lets authorised users perform institutional work through text or voice commands. A master agent plans, checks permissions, calls registered tools through a policy gateway, verifies, and reports. An internet intelligence agent adds source-backed public information about the institution. The original federated read-only connector path remains available alongside.

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

The client assistant (`apps/web/assistant/`, served at `/`) and the developer platform console (`apps/web/console/`, served at `/console/`) are separate apps that share only the sign-in client and base styles (`apps/web/shared/`); neither links to the other. The console is served outside production by default and is off in production unless `SAFFRON_WEB_CONSOLE_ENABLED=true`, so a client-facing deployment serves only the assistant. With OIDC, register both `https://<host>/` and `https://<host>/console/` as callback and sign-out URLs, since each app returns to itself after sign-in.

`make platform-smoke` runs that sequence end to end. Local defaults need no external services (SQLite, local object store, inline job queue, hashing embeddings, deterministic planner, outbox email). See `.env.example` for PostgreSQL, S3, SQS, OCR, SMTP/SES, embeddings, model planner, and the Tavily-backed internet intelligence provider.

## Platform surface

- `POST /v1/ingestion/uploads`, `/v1/ingestion/sheets`, job inspection, mapping approval, duplicate review, commit, import reports. Approving a mapping, committing and the last duplicate decision answer with the job as it stands and run on the job queue; follow it with `GET /v1/ingestion/jobs/{job_id}`.
- `GET /v1/data/*` unified, minimised data access (students, attendance, fees, exams, faculty, programs, events, admissions).
- `POST /v1/agent/commands` master agent (text or voice transcripts), `/v1/agent/tools`, approvals for high-risk actions, background jobs, run history.
- `POST /v1/documents` and `/v1/documents/search` document intelligence with page-level citations; `POST /v1/documents/evaluate` scores the pipeline against labelled questions.
- `PUT /v1/intelligence/profile`, `POST /v1/intelligence/investigate`, mentions, digest, monitoring runs.
- `GET /v1/reports` and `/v1/reports/{id}/download` generated reports, notifications, email outbox, institutions.

The `generate_report` tool writes records as CSV, Excel, PDF, Word (`.docx`) or PowerPoint (`.pptx`), with no extra dependencies (`apps/api/app/actions/files.py`). The deterministic planner picks Word for "Word document", "docx" or "MS Word" and PowerPoint for "PowerPoint", "pptx", "presentation" or "slides". A Word file holds up to 5,000 rows and a deck up to 240 rows and 10 columns, 12 rows per slide; past that the file says how many rows it leaves out and suggests Excel.

## Document RAG pipeline

    Documents -> Chunking -> Embedding -> Vector store
                                              |
    Query -> Embed query -> Retrieve top-K -> Rerank -> Top-N -> Model -> Cited answer
                                                                            |
                                                                    Evaluate (RAG evals)

| Stage | Code | Configuration |
| --- | --- | --- |
| Chunking | `documents/chunking.py`: 900-character chunks with 150 characters of overlap. Each chunk keeps its page, and headings stay with their paragraph. | |
| Embedding | `documents/embeddings.py`: local hashing, Ollama or OpenAI-compatible. | `SAFFRON_EMBEDDING_PROVIDER` |
| Vector store | `documents/vector_store.py`: `InstitutionVectorStore` scores stored chunk vectors with a hybrid score (0.7 cosine + 0.3 query-term overlap). It filters by classification and category before scoring. Another backend only has to implement `VectorStore.search`. | |
| Retrieve top-K | `DocumentRagService.retrieve` | `SAFFRON_RAG_RETRIEVE_K` (default 20), or `retrieve_k` per request |
| Rerank → top-N | `documents/rerank.py`: `LexicalReranker` (BM25 plus query-term coverage and phrase matches, runs locally) or `HttpReranker` (the Cohere, Jina, Voyage, LiteLLM or TEI `/rerank` endpoint). If the reranker fails, retrieval order is kept and the answer carries a warning. | `SAFFRON_RERANK_PROVIDER` = `lexical` \| `http` \| `none`; `top_k` per request (top-N, at most 10); `SAFFRON_RAG_MIN_RERANK_SCORE` drops weak passages |
| Model → answer | `DocumentRagService.answer`: the model sees only the top-N passages. Its answer must cite them, or the answer falls back to the quoted passages. The response's `pipeline` field reports each stage. | the planner model |
| Evaluate | `documents/evals.py`: `RagEvaluator` | see below |

**RAG evals.** An eval case is a question plus labels:
- `expected_document_ids`, `expected_pages` or `expected_text` say which passage is right.
- `reference_answer` and `must_contain` describe a correct answer.
- `answerable: false` marks a question the pipeline should decline.

For each case the evaluator reports:
- **Retrieval** (hit rate, recall, precision, MRR, nDCG), measured on the top-K candidates and again on the reranked top-N, so the reranker's lift is visible.
- **Generation:** faithfulness, answer relevance, context recall, answer recall and F1 correctness, keyword coverage, citation validity and coverage, and abstention.

The scores are deterministic, so they run in CI. Passing a `judge` model to `RagEvaluator` adds model-graded faithfulness, relevance and correctness.

    make rag-eval                                                        # sample set in evals/rag/
    PYTHONPATH=apps/api python scripts/rag_eval.py --reranker none       # compare without reranking
    PYTHONPATH=apps/api python scripts/rag_eval.py --min-rerank-score 0.05 --min-pass-rate 1.0 --json
    curl -X POST http://localhost:8000/v1/documents/evaluate -H 'Authorization: Bearer dev-token' -H 'X-Demo-Role: principal' -H 'Content-Type: application/json' \
      -d '{"top_k":3,"cases":[{"question":"What is the late fee?","expected_text":"late fee","must_contain":["100"]}]}'

`scripts/rag_eval.py` ingests `--documents` into an in-memory store using the configured embeddings and reranker. Cases can name files with `expected_files`. The script exits non-zero when the pass rate is below `--min-pass-rate`. The endpoint needs `documents:manage` and takes at most 100 cases per run.

## Open-task agent

When a command asks for something the registered tools cannot make, the master agent hands it to the open-task agent (off by default; `SAFFRON_OPEN_TASK_ENABLED=true`). It covers presentations and Word documents richer than the tables `generate_report` lays out, charts, workbooks with formulas or several sheets, and custom analyses, comparisons, timetables or drafts built from the records. When it is enabled, presentation and Word requests go to it; when it is off, `generate_report` makes them as Word or PowerPoint tables. The agent runs on Claude Opus 5.5 (`SAFFRON_OPEN_TASK_MODEL_ID`) through the Claude API or Amazon Bedrock:

    User request -> master agent (planner)
       |- a registered tool fits    -> runs as before (fast, cheap)
       '- the tools cannot do it    -> open-task agent
             |- reads records through the tool gateway, as the person who asked
             |- writes and runs Python in a sandbox (openpyxl, python-pptx, python-docx, matplotlib)
             '- files in outputs/ become downloadable reports + a short summary

- **Same rules as the tools.** It only gets the read-only tools the person's role holds. Every call goes through the gateway: capabilities, scope, consent, field minimisation and audit. It cannot send email, notify anyone or change a record; the person can ask the assistant to send the file afterwards. Its files need `reports:generate`, and anyone other than the creator needs every data permission the creator held to download them.
- **Figures come from code.** Each tool result is saved into the task's workspace as JSON (and CSV for tables). Claude sees a summary and a short preview and computes figures with code from the files. The workspace is deleted when the task ends.
- **Sandbox.** `SAFFRON_OPEN_TASK_SANDBOX=isolated` (the default, and the only mode production accepts) runs the code in its own user, network, PID and mount namespaces through `unshare`. It has no network, no view of the API process, and empty mounts over the service's files. It also gets a scrubbed environment, CPU, memory and file-size limits, and a timeout. The host must allow unprivileged user namespaces. `guarded` is for development machines that do not.
- **Limits.** Turns, a deadline and a daily allowance per person (`SAFFRON_OPEN_TASK_*` in `.env.example`). With a thread or SQS queue, tasks run as background jobs and the person is notified with the download links.
- With `SAFFRON_AGENT_PLANNER=model`, the planner can also hand work over itself. Otherwise the rules in `apps/api/app/open_task/routing.py` decide: a format the tools cannot make, or unmapped work with a deliverable. Greetings and general questions still go to free conversation.

Install the libraries with `uv sync --extra open-task`; the API image includes them.

## Assistant page

The client assistant (`apps/web/assistant/`, served at `/`) is a chat page in saffron colours with light and dark themes. It follows the device's theme unless one is chosen in the account menu.

- **Chat history on the device.** A sidebar holds New chat, search and past chats grouped by date (Today, Yesterday, Previous 7 days and older), each of which can be renamed or deleted. Chats are kept in the browser's IndexedDB, separately for each signed-in account (keyed by the OIDC subject), up to 300 chats. The server still stores no transcript. "Delete all chats on this device" in the account menu removes them.
- **Answers.** Markdown in answers is rendered with DOM calls, never `innerHTML`. Sources are listed in a collapsible section. Each message has Copy, Read aloud and Retry.
- **Files.** Files the agent made appear in the chat as file cards (Excel, Word, PowerPoint, PDF, CSV) and download through the signed-in client. "Your files" in the sidebar lists generated reports (`GET /v1/reports`). A background open-task job is followed by polling `GET /v1/agent/jobs/{id}`; its answer and files appear in the chat when it finishes.

## Voice conversation

The assistant page holds a two-way spoken conversation, started with the Voice button in the composer. The microphone stays on while Agent Saffron speaks, so the person can talk over a reply: it stops at once and the new question is answered. Saying "stop", "ruko", "bas" or "ನಿಲ್ಲಿಸು" only silences it. Phrases said with a short pause become one request, and the echo of Agent Saffron's own voice is ignored. On iPhone and iPad, where the microphone and speaker cannot both run, it listens between replies and shows a Stop button.

- **Free conversation.** Greetings, general questions and follow-ups get a real reply. Anything the master agent cannot map becomes conversation with the configured model instead of "could not map this request". Institutional figures come only from the tools. The recent turns travel with each request; the server stores no transcript.
- **Internet by voice.** "Search the internet for …", "latest news on …", "weather in …" (also in Hindi and Kannada) run an open-web search through the Tavily settings, with cited sources on screen. Results are untrusted data, queries carrying personal data are refused, and each person has a daily allowance (`SAFFRON_WEB_SEARCHES_PER_PERSON_PER_DAY`). Questions about the institution's own presence online ("Google reviews of our college") still go to the internet-intelligence tools; when the agent has none set up, people allowed to use them get this open-web search instead, with the college's name in place of "our college".
- **Fast replies.** A model reply is streamed: the first sentence is spoken while the model is still writing the rest, and the words appear on screen as they are said. A turn that takes a moment gets a short "one moment" (`SAFFRON_VOICE_FILLER_AFTER_MS`). If a reply fails after it started speaking, it is withdrawn and a standard reply is spoken instead.
- **Indian voice and languages.** Replies come back in the language spoken: Indian English, Hindi (Devanagari or Hinglish) or Kannada. By default the device's own Indian male voice speaks (Prabhat, Madhur and Gagan in Edge; Ravi and Hemant on Windows; Rishi on Apple), falling back to another Indian voice when the device has no male one. With `SAFFRON_VOICE_TTS_PROVIDER=polly` they are spoken by Amazon Polly's Kajal (neural, en-IN and hi-IN), sentence by sentence; Polly has no male Indian voice, so this switches to a woman's voice. Kannada is always spoken by the device.
- **Protocol.** `POST /v1/voice/sessions` returns a one-use ticket. The client opens `/v1/voice/sessions/{id}/stream`, authenticates with the ticket, and opts into `thinking`, `speech` and `interrupt`. It then sends `utterance` (text, language, history) and `interrupt`, and receives `thinking`, `speech` (sentence text, and Polly MP3 when configured; streamed replies send their first sentences before `answer`), `answer`, `speech_end` and `cancelled` (`retracted` when a streamed reply failed after speaking). A turn that reached the master agent is never cut short; it finishes and is shown without being spoken. Sessions are rows in the control database, so any API task can serve the socket.
- **Before a demo.** Run `PYTHONPATH=apps/api python scripts/voice_preflight.py` in the deployed task, with the API's environment. It makes one call each to Polly, the conversation model and Tavily and says which are working, which are off and which would silently fall back; it exits 1 if a configured service fails.

## Current state

The repository now contains the complete all-phase reference implementation for the defined contracts:

- P0 security and contracts: deny-by-default principals, capabilities, College/Department/Batch scope, verified OIDC/JWKS boundary, consent/revocation safeguards, redaction, request limits, rate limiting, security headers, and read-only SQL safety.
- P1 institution boundary: an authenticated `apps/connector` service with health/readiness, approved semantic tools, server-to-server bearer authentication, scope/consent enforcement, effective-scope attestation, provenance, redaction markers, a local aggregate repository, and a PostgreSQL reporting-view adapter.
- P2 policy: a strict Cerbos HTTP decision-point adapter and versioned Cerbos sidecar policy/configuration under `infra/cerbos/`; local policy remains available for isolated tests.
- P3 control plane: SQLite and PostgreSQL stores for audit, source health, briefing history, answer-envelope hashes/counts, model attempts, retention pruning, and retry-safe outbox delivery.
- P4 evidence/streaming: provenance-aware results and closed, versioned SSE events with contiguous ordering (`message_start`, citation/warning, delta, answer, `message_end`, `done`), plus closed WebSocket voice input.
- P5 providers/operations: deterministic and Ollama adapters, optional LiteLLM streaming/usage adapter, redaction-safe trace export, provider capability declarations, and production configuration guards.
- P6 edge/integration boundaries: trusted Open edX/Moodle identity adapters, fail-closed OpenFGA checker, allowlisted MCP/agent-gateway targets, and LiveKit/Pipecat-style voice event normalization without raw-audio persistence.

The College A deterministic connector remains available only for development/tests. The API also supports a deployment-managed `SAFFRON_INSTITUTION_CONNECTORS` JSON registry so one universal read-only contract can route to multiple college connector services; each entry supplies its own source ID, institution ID, endpoint, approved tools, and secret-environment reference. The legacy single-connector variables remain supported. The reference compose stack adds PostgreSQL, Cerbos, and the authenticated connector service; it does not create or contact a real college system. The browser voice path receives final transcripts only, rejects binary audio frames, uses one-use tickets shared through the control database, and keeps bounded session, idle and utterance limits.

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

Copy `.env.example` and keep `SAFFRON_ENVIRONMENT=development` for the local deterministic path. The development identity uses `Authorization: Bearer dev-token` plus explicit demo headers. To exercise the remote connector path, disable `SAFFRON_ENABLE_DEMO_DATA`, configure the connector URL/token, enable scope attestation, and use the service in `infra/docker/compose.dev.yml`. To exercise Cerbos, set `SAFFRON_PDP_MODE=cerbos` and configure `SAFFRON_CERBOS_URL`.

LiteLLM, public-web search, edge adapters, OpenFGA, trace export, MCP/agent gateway, and LiveKit/Pipecat are explicit adapters. They are not enabled by default and require deployment-managed endpoints, credentials, network policy, evaluation, and operational approval. Production configuration rejects the development identity, deterministic demo data, SQLite control-plane defaults, missing OIDC, missing connector attestation, non-TLS connector/Cerbos URLs, deterministic model selection, and non-fail-closed audit settings.

## Renamed from Guru Ji

The product was called Guru Ji. Settings are now read as `SAFFRON_*` (for example `SAFFRON_ENVIRONMENT`, `SAFFRON_MODEL_PROVIDER`). A deployment still configured with `GURU_*` names keeps working: when the API loads, each `GURU_*` variable is copied to its `SAFFRON_*` name if that name is unset, so a `SAFFRON_*` value always wins (`apps/api/app/config/legacy_env.py`). The connector service reads `SAFFRON_CONNECTOR_*` and falls back to `GURU_CONNECTOR_*`. New configuration should use `SAFFRON_*`.

Names shared with systems outside this repository keep the old spelling:

- OIDC/Cognito claims `guru_role`, `guru_capabilities`, `guru_scopes`, `guru_parental_consent` and `guru_revoked`, and their `custom:` forms.
- College database reporting views `public.guru_student_overview` and `public.guru_attendance_summary`.
- Website ownership verification: the `guruji-verification` meta tag, the `guruji-verification=TOKEN` DNS TXT record and `/.well-known/guruji.json`.
- Inter-service headers `X-Guru-Edge-Session` and `X-Guru-Connector-Contract`, and the LMS identity paths `/api/guru/identity` and `/local/guru/identity`.
- Cerbos policy versions `guru-cerbos-v1` and `guru-local-v1`, and the development-only suppression key `guru-ji-development-only`.

The crawler now sends the User-Agent `AgenticSaffron-InstitutionIntelligence/1.0`; robots.txt groups written for `GuruJi-InstitutionIntelligence` still apply to it.

## Deployment

The API runs on the Amazon Lightsail container service `agentic-saffron` in `ap-south-1`, served at https://agentic-saffron.sast-skills.com. Each push to `main` that changes the app runs `.github/workflows/deploy-agentic-saffron.yml`, which:

1. builds `infra/docker/Dockerfile.api`;
2. pushes the image to the ECR repository `agentic-saffron-api`, tagged with the commit;
3. copies the running Lightsail deployment with only the image changed, then waits until the new deployment passes its health check (`/v1/health/live`).

If the new deployment fails, Lightsail keeps the previous one serving and the workflow fails. The service's environment, ports and health check are managed in Lightsail, not in this repository, so the workflow never reads or changes them. The workflow signs in to AWS through GitHub OIDC as `agentic-saffron-github-deploy`. That role can only push to that ECR repository and deploy that one container service. The job runs once the repository variable `AWS_DEPLOY_ROLE_ARN` holds that role's ARN; `workflow_dispatch` redeploys `main` by hand.

## Example request

    curl -X POST http://localhost:8000/v1/chat \
      -H 'Authorization: Bearer dev-token' \
      -H 'X-Demo-Principal: student-1' \
      -H 'X-Demo-Role: student' \
      -H 'X-Demo-College: college_a' \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"What is the current attendance summary?","institution_scope":{"college_id":"college_a"}}'

No production deployment, external institutional mutation, hosted-model request, or credentialed real integration is claimed by this build workflow.
