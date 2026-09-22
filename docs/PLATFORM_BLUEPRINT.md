# AI-powered institutional intelligence platform — implementation map

This document maps the platform blueprint (ingestion → canonical database → tool gateway → master agent → internet intelligence) to the code in this repository. It complements `ARCHITECTURE.md`, which describes the original federated read-only assistant; both paths share identity, policy, audit, streaming, and voice.

## The one rule everything follows

The language model never becomes the database and never talks to it. Every fact a model sees arrives through a registered tool that the gateway has authorised, validated, executed, minimised, and audited:

```
LLM / planner ──tool call──▶ ToolGateway ──▶ InstitutionDataService ──▶ InstitutionDataStore ──▶ SQL
                                │
                     authorize() + PDP + argument schema + approval + field policy + audit
```

There is no `execute-sql` tool. `app/institution_data/store.py` is the only module that touches canonical tables, every method takes an `institution_id`, and PostgreSQL row-level security (`migrations/002_institution_data.sql`) is bound to `app.institution_id` for a second, database-level fence.

## Layer by layer

| Blueprint section | Implementation | Notes |
| --- | --- | --- |
| Data collection & ingestion API | `app/api/routes/ingestion.py`, `app/ingestion/service.py` | Multipart uploads, Google Sheets export, existing-database connector; jobs run through the job queue. |
| Excel / CSV / JSON | `app/ingestion/parsers/csv_parser.py`, `excel_parser.py`, `json_parser.py` | The XLSX reader is dependency-free (zip + XML); `openpyxl`/`xlrd` are optional extras. |
| PDF (text, table, scanned, mixed) | `app/ingestion/parsers/pdf_parser.py` | Page-by-page decision; PyMuPDF/pdfplumber/pypdf when installed, OCR fallback per page, explicit `needs_ocr` warnings otherwise. |
| Word | `app/ingestion/parsers/docx_parser.py` | Paragraphs, headings, tables from `word/document.xml`. |
| Images / scans / OCR | `app/ingestion/parsers/image_parser.py`, `ocr.py`, `text_layout.py` | Tesseract CLI and AWS Textract adapters; layout→table and form field extraction; OCR is never trusted automatically. |
| AI data mapping engine | `app/normalization/mapping.py`, `canonical.py` | Synonym/token/fuzzy/value-shape scoring, entity detection, conflicts and abbreviations to review, optional model suggestions that never auto-apply, approved profiles reused per header signature. |
| Intermediate data model | `app/ingestion/models.py`, `ingestion_records` table | Every source row keeps file, sheet/page, row number, raw fields, normalised fields, issues, and planned action. |
| Cleaning | `app/normalization/cleaning.py` | Safe normalisation only (whitespace, case, phone, email, semester words, program aliases, dates); ambiguous dates flagged, nothing invented. |
| Validation | `app/normalization/validation.py` | Required fields, formats, ranges, cross-field checks, OCR confusable-character suspicion using the column's dominant identifier pattern. |
| Deduplication | `app/normalization/deduplication.py` | Deterministic natural keys first, then name similarity corroborated by DOB/phone/email; uncertain matches go to human review. |
| Canonical database & multi-tenancy | `app/normalization/canonical.py`, `app/institution_data/` | 12 entities generated into tables; `institution_id` + `record_key` unique; lineage columns; RLS on PostgreSQL. |
| Data stores by purpose | SQLite/PostgreSQL (`persistence/sql_backend.py`), object store (`storage/object_store.py`: memory/local/S3), vectors as JSON (pgvector-ready), Redis/SQS optional | `workers/queue.py` implements inline, thread, and SQS queues. |
| Unified data access API | `app/data_access/service.py`, `app/api/routes/data.py` | Domain methods only; field policy (`field_policy.py`) applies data minimisation per role. |
| Tool registry & gateway | `app/gateway/`, `app/platform_tools/` | 26 tools with closed argument schemas, permission, risk level, audit; `ToolGateway.invoke` is the single execution path. |
| Data minimisation | `field_policy.py` + `ToolGateway` | Counts return `{count}`; lists are bounded; contact fields need `students:read_contact`; the gateway strips student contact fields from any output as a second pass. |
| Document RAG | `app/documents/` | Chunking with page numbers, hashing/Ollama/OpenAI-compatible embeddings, classification-aware retrieval, cited answers, model output rejected when it does not cite. |
| Master agent & specialists | `app/agents/` | Deterministic planner (entity extraction + intent rules) and model planner (validated JSON), bindings between steps, data/action/internet specialists, verification, audit and run history. |
| Action agent | `app/actions/` | CSV/XLSX/PDF generation, email (outbox/SMTP/SES) restricted to directory members and allowed domains, notifications; record updates are high-risk and require single-use approvals. |
| Voice | `app/api/routes/voice.py`, `voice/protocol.py` | `mode: "agent"` (or `GURU_VOICE_AGENT_MODE=agent`) routes final transcripts through the master agent; long work is accepted and completes in the background with a notification. |
| Internet intelligence | `app/internet_intelligence/` | Profiles, query generation, open-web search (Tavily adapter + fixture provider), robots-aware fetching, URL dedupe, entity resolution (high/medium/low/not matched), relevance and time filters, source classification, evidence store, source-backed summaries. |
| Continuous monitoring | `internet_intelligence/monitoring.py`, `scripts/run_monitor.py` | New/changed/duplicate detection, importance, daily digest, alerts to profile recipients. |
| Unified intelligence | `MasterAgent` + `internet_investigate` + `get_institution_summary` | "Complete update" commands combine internal indicators with public findings, each cited. |
| Security & audit | `auth/roles.py`, `gateway/gateway.py`, control-plane audit tables | Seven roles; every tool invocation, denial, approval, and agent run is audited without persisting prompts or tool data. |
| Data lineage & re-imports | `institution_data/models.py`, `IngestionService.commit` | Each record knows file, sheet/row, job; re-imports report new / updated / unchanged / skipped. |

## Roles and capabilities

| Role | Adds |
| --- | --- |
| student | ask, voice, agent commands, document search |
| faculty | + student/attendance/exam reads, reports, intelligence read, briefings |
| staff | + data ingest/review, fees, notifications |
| hod | faculty + contact fields, fees, faculty directory, email, notifications |
| principal | hod ∪ staff + document management, record writes, intelligence management |
| institution_admin / main_admin / platform_super_admin / system | everything, including access management |

Roles come from verified OIDC claims, a trusted LMS edge, or development demo headers. `auth/roles.py` is the single mapping; `infra/cerbos/policies/platform_tool.yaml` mirrors it for the Cerbos decision point.

## Request flows

**Ingestion**: upload → object store (`{institution}/raw/{file}/{name}`) → `ingestion_files` + job → parse → select table → mapping proposal (profile reuse) → `mapping_review` if uncertain → apply mapping → clean → validate → deduplicate → `duplicate_review` if needed → ready → commit (upsert with lineage) → import report → notification.

**Command**: `POST /v1/agent/commands` → capability and scope check → plan (deterministic or model) → for each step: resolve bindings → gateway (authorise, validate, approve, execute, minimise, audit) → verify → compose answer (optional model wording with numeric guard) → sources, artifacts, warnings → audit + run history.

**Internet intelligence**: profile → queries → search provider → canonical URL dedupe → fetch (robots, size, content-type) or snippet → entity resolution → relevance → date window → source type → evidence store → deterministic or cited model summary → report.

## Configuration

See `.env.example`. Local defaults need no external service: SQLite, local/in-memory object store, inline queue, hashing embeddings, deterministic planner, outbox email, OCR disabled. Production validation requires PostgreSQL for `INSTITUTION_DATABASE_URL`, S3 object storage, a thread or SQS queue, and TLS for SMTP.

## Operations

- `make run` starts the API; `/platform.html` is the platform console; `/` is the assistant.
- `make worker` drains background jobs (required for `GURU_JOB_QUEUE=sqs`; optional alongside the thread queue).
- `make monitor` runs one monitoring pass for every institution with monitoring enabled (schedule with cron/EventBridge).
- `make platform-smoke` exercises upload → import → command → report against a running development API.
- `make platform-postgres-smoke` (with `CONTROL_DATABASE_URL` pointing at a disposable PostgreSQL database) verifies the canonical store, ingestion state, the intelligence store, and that row-level security hides rows without a tenant context. Set `GURU_SMOKE_INSTITUTION` to run `platform_smoke.py` against a persistent database with a fresh institution.
- `make openapi` regenerates `openapi.yaml`; `make validate-openapi` checks it against the live routes.

## Phases delivered

| Phase | Status |
| --- | --- |
| 1 Data foundation (API, DB, object store, Excel/CSV/basic PDF) | done |
| 2 Universal ingestion (DOCX, PDF tables, OCR adapters, images, Sheets, DB connectors) | done (OCR engines and PDF libraries are optional installs) |
| 3 Unified API | done |
| 4 AI agent (master agent, registry, data/action agents, gateway, audit) | done |
| 5 Document intelligence (extraction, embeddings, RAG, sources) | done |
| 6 Voice (agent mode on the existing transcript transport) | done |
| 7 Internet intelligence | done (Tavily adapter; other providers implement one protocol) |
| 8 Continuous monitoring | done (scheduling is deployment-managed) |

## What is deliberately not built

- No arbitrary SQL tool, no raw connection strings from users or models.
- No silent OCR trust: every OCR row is flagged and its identifiers pattern-checked.
- No emails to arbitrary addresses: recipients must come from the institution directory or an allowed domain.
- No record change without an explicit, single-use approval.
- No claims from social content: findings are attributed to their sources and marked untrusted.
