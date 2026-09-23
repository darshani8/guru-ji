# Production onboarding checklist: first real college connector

**Status:** Draft for controlled onboarding; no real college system, production credential, or production deployment has been connected by this repository work.

**Purpose:** Move one approved college from contract review through a limited production canary without widening institutional access, placing secrets in source control, or confusing a green local test suite with production readiness.

**Scope:** One college, one institution-local connector service, read-only aggregate reporting, and the central Guru Ji API registry. This checklist does not authorize a merge, deployment, credential creation, data migration, or external system change by itself.

## Non-negotiable boundaries

- Use the institution's approved reporting views, not raw student or identity tables.
- Permit only the reviewed semantic tools: `institution.overview`, `institution.attendance_summary`, and `institution.source_health`.
- Keep `source_id`, `institution_id`, endpoint metadata, and secret-environment names in deployment configuration; keep token and database-password values only in the approved secret manager.
- Do not place real credentials, JWTs, student records, prompts, completions, or raw audio in this repository, tickets, screenshots, CI logs, or chat.
- The connector is read-only. No institution write, arbitrary SQL, arbitrary tool URL, or browser-supplied role/capability is part of this onboarding.
- A failed identity, policy, scope, consent, freshness, audit, or dependency check blocks promotion. Do not fall back to deterministic demo data in production.
- Preserve the College → Department → Batch scope hierarchy and immutable source/institution identifiers throughout the request, connector response, provenance, audit, and rollback evidence.

## 0. Named owners and release record

Record the names, contact paths, and approval date before collecting credentials.

- [ ] Release owner: `____________________`
- [ ] Institution data owner: `____________________`
- [ ] Institution connector/network owner: `____________________`
- [ ] Identity/OIDC owner: `____________________`
- [ ] Security/privacy reviewer: `____________________`
- [ ] Platform/SRE/on-call owner: `____________________`
- [ ] Rollback owner and incident channel: `____________________`
- [ ] Approved maintenance/canary window: `____________________`
- [ ] Target RTO/RPO, latency budget, error budget, and rate/concurrency limits recorded.
- [ ] Written institution approval, privacy/data-processing approval, and security review attached to the release record.
- [ ] A production change ticket links this checklist, the reviewed commit/image digests, and the final sign-off.

## 1. Identify the first college and freeze the contract

Use stable identifiers approved by the institution. Do not invent identifiers from a display name after deployment begins.

- [ ] `institution_id`: `____________________`
- [ ] `source_id` (globally unique in the Guru Ji deployment): `____________________`
- [ ] Connector display name: `____________________`
- [ ] Approved College ID: `____________________`
- [ ] Approved Department IDs, if the first rollout is narrower than the college: `____________________`
- [ ] Approved Batch IDs, if the first rollout is narrower than the department: `____________________`
- [ ] The same IDs are mapped and verified in the institution source, connector allowlist, API registry, identity claims, policy checks, reports, and audit evidence.
- [ ] The source is not a duplicate of a legacy single-connector entry or another registry entry.
- [ ] The approved tool list and response/row/time limits are frozen for the canary.
- [ ] The institution confirms reporting periods, freshness expectations, aggregate definitions, and handling of incomplete or stale data.

### Required contract fields

- [ ] `contract_version=2` is accepted by the connector.
- [ ] Every request contains a verified principal identity and the requested College/Department/Batch scope.
- [ ] The connector returns `source_id`, `tool_name`, `effective_scope`, provenance, retrieval time, reporting period, completeness, row count, and redaction markers.
- [ ] The connector does not return names, emails, phone numbers, addresses, raw identity records, tokens, or unapproved student-level data.
- [ ] Partial, stale, unavailable, refused, or out-of-scope results remain explicit and are not silently reported as successful fresh data.

## 2. Confirm the institution reporting boundary

The current PostgreSQL adapter expects institution-owned reporting views. If the college uses another schema or view contract, stop and review the adapter change before onboarding; do not hide a schema mismatch in deployment configuration.

- [ ] `public.guru_student_overview` exists, or an institution-approved reviewed equivalent has been implemented and tested.
- [ ] `public.guru_attendance_summary` exists, or an institution-approved reviewed equivalent has been implemented and tested.
- [ ] Required aggregate columns and types are documented and validated:
  - [ ] College scope identifier.
  - [ ] Department and Batch scope identifiers where applicable.
  - [ ] `active_students`.
  - [ ] `active_departments`.
  - [ ] `attendance_rate_percent`.
  - [ ] Department identifier/name and aggregate attendance rate for summary rows.
  - [ ] Reporting-period start/end and freshness semantics.
- [ ] The database role used by the connector has `SELECT` only on the approved reporting views.
- [ ] The connector role cannot read raw student/identity tables or execute `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `CREATE`, or `DROP`.
- [ ] Scope predicates for College, Department, and Batch have been tested against cross-college and cross-department fixtures.
- [ ] Query audit confirms that only approved views and bounded queries are used.
- [ ] Database connection limits, TLS/CA policy, backup/restore owner, and network route are recorded.
- [ ] The institution data owner has approved the minimum fields and aggregate granularity.

## 3. Build and verify the institution-local connector

Deploy `apps/connector` inside the institution-approved network boundary or an equivalently controlled private service path.

- [ ] Build from the exact reviewed PR head and record the immutable image digest.
- [ ] `GURU_CONNECTOR_ENVIRONMENT=production` is set.
- [ ] `GURU_CONNECTOR_SOURCE_ID` equals the approved `source_id`.
- [ ] `GURU_CONNECTOR_INSTITUTION_ID` equals the approved `institution_id`.
- [ ] `GURU_CONNECTOR_ALLOWED_COLLEGE_ID` equals the approved College ID.
- [ ] `GURU_CONNECTOR_ALLOWED_DEPARTMENTS` and `GURU_CONNECTOR_ALLOWED_BATCHES` are populated when the canary is intentionally narrower than the college.
- [ ] `GURU_CONNECTOR_DATABASE_URL` is injected by the secret manager and points to PostgreSQL.
- [ ] `GURU_CONNECTOR_SERVICE_TOKEN` is injected by the secret manager and is not `connector-dev-token`.
- [ ] No demo repository is active in the production connector.
- [ ] The service is reachable only through the approved HTTPS/private route; certificate, hostname, firewall, and service-to-service policy are verified.
- [ ] Health and readiness checks pass:
  - [ ] `GET /v1/health` returns `status=healthy` for the configured source.
  - [ ] `GET /v1/ready` returns `ready=true` only when the reporting database is reachable.
  - [ ] `POST /v1/execute` accepts only the reviewed contract and approved tools.
- [ ] Startup, access, and error logs contain no service token, database URL/password, JWT, or raw institutional record.
- [ ] A restart, dependency outage, and database-unavailable test produce a safe not-ready/unavailable state.

## 4. Configure secrets and trust boundaries

Secrets are references in deployment configuration and values in the secret manager—not values committed to this file or the repository.

- [ ] Central API OIDC issuer, audience, JWKS URL, and approved signing algorithms are configured.
- [ ] OIDC claim mapping for role, capability, College/Department/Batch scope, consent, and revocation is approved.
- [ ] JWKS rotation, clock skew, issuer mismatch, audience mismatch, expired token, and invalid-signature behavior are tested.
- [ ] Cerbos is deployed at the approved pinned policy version (`guru-cerbos-v1`) over HTTPS and is configured fail-closed.
- [ ] The API-to-connector bearer token is stored and rotated by the approved secret manager.
- [ ] The connector's database URL/password is stored and rotated by the approved secret manager.
- [ ] Secret access is limited to the intended API/connector workloads; humans receive no unnecessary read access.
- [ ] Rotation and revocation procedures have been rehearsed without placing old/new values in logs or tickets.
- [ ] `GURU_AUDIT_FAIL_CLOSED=true` is set for production.
- [ ] Trace/log export, if enabled, is redaction-reviewed and has an owner and retention policy.

## 5. Register the first college in the central API

Use a deployment-managed registry. The following is a shape example only; replace placeholders through the deployment system and never commit secret values.

```text
GURU_ENVIRONMENT=production
GURU_ENABLE_DEMO_DATA=false
GURU_CONNECTOR_SCOPE_ATTESTATION_REQUIRED=true
GURU_PDP_MODE=cerbos
GURU_CERBOS_URL=https://<approved-private-cerbos>
GURU_CERBOS_POLICY_VERSION=guru-cerbos-v1

GURU_INSTITUTION_CONNECTORS=[
  {
    "source_id": "first_college_remote",
    "institution_id": "first_college",
    "display_name": "First College connector",
    "base_url": "https://<approved-private-connector>",
    "allowed_tools": [
      "institution.overview",
      "institution.attendance_summary",
      "institution.source_health"
    ],
    "auth_token_env": "FIRST_COLLEGE_CONNECTOR_TOKEN",
    "scope_attestation_required": true
  }
]

FIRST_COLLEGE_CONNECTOR_TOKEN=<injected-at-runtime-by-secret-manager>

# Institutional data platform (ingestion, canonical data, agents). Off in
# production unless enabled here; when enabled, every line below is required
# and startup validation fails without them.
GURU_PLATFORM_ENABLED=true
INSTITUTION_DATABASE_URL=postgresql://<user>:<password>@<approved-private-host>/<database>
GURU_OBJECT_STORE=s3
GURU_S3_BUCKET=<approved-bucket>
GURU_JOB_QUEUE=sqs
GURU_SQS_QUEUE_URL=https://sqs.<region>.amazonaws.com/<account>/<queue>
```

- [ ] The real `source_id`, `institution_id`, display name, HTTPS endpoint, and secret reference are approved before release.
- [ ] The registry contains no token value, password, query string, URL credentials, or fragment.
- [ ] The endpoint is HTTPS in production and resolves only to the approved connector service.
- [ ] The registry has one source for the first college and no accidental legacy duplicate.
- [ ] The three approved tools are explicit and no unsupported tool is present.
- [ ] The registry entry requires scope attestation.
- [ ] `CONTROL_DATABASE_URL` points to the approved PostgreSQL control plane.
- [ ] OIDC is configured; the development bearer token is replaced; allowed browser origins are exact and non-wildcard.
- [ ] The configured model provider is approved and non-deterministic for production; provider credentials remain server-side.
- [ ] Configuration validation passes before deployment and startup fails rather than widening scope or enabling demo data.
- [ ] If the data platform is enabled (`GURU_PLATFORM_ENABLED=true`), `INSTITUTION_DATABASE_URL` is PostgreSQL, `GURU_OBJECT_STORE=s3` with `GURU_S3_BUCKET`, and `GURU_JOB_QUEUE` is `thread` or `sqs` (with `GURU_SQS_QUEUE_URL` and a running `make worker` for `sqs`); the workload role holds the S3 and SQS permissions. Leave it unset (off) to deploy the assistant alone.

## 6. Run the repository and artifact gates

The exact artifact must be traceable to the reviewed commit. Store command output and links in the release record, not secrets in this checklist.

- [ ] CI is green for the reviewed PR head.
- [ ] Dependency lockfile and vulnerability scan are clean or formally risk-accepted.
- [ ] Unit and route tests pass.
- [ ] Python compilation, OpenAPI validation, lint, type checks, and repository hygiene pass.
- [ ] API and connector images build reproducibly.
- [ ] SBOM and image scan are attached; critical findings block promotion.
- [ ] No `.env` file, local database, cache, test token, prompt/completion, or generated credential is in the build artifact.
- [ ] Image digests, base image versions, lockfile hash, migration revision, and deployment manifest revision are recorded.

## 7. Validate in integration/staging before real data

Use synthetic data first. If institution-approved masked data is required, record the data owner's approval and retention/deletion date.

### Service and dependency checks

- [ ] API liveness and readiness pass; readiness reports PostgreSQL, not SQLite.
- [ ] Connector health/readiness pass against the institution-shaped reporting database.
- [ ] Cerbos health and policy-version/freshness checks pass.
- [ ] OIDC test issuer/JWKS and the model gateway are reachable through approved private paths.
- [ ] Restart and dependency-outage behavior is safe and observable.

### Authorization and isolation matrix

- [ ] Main admin with approved College scope: only granted capability/actions are allowed.
- [ ] Faculty with approved Department/Batch scope: request outside scope is denied.
- [ ] Consented student with approved Batch scope: permitted read-only request is allowed.
- [ ] Student without verified consent for a Batch-scoped request: denied before connector query.
- [ ] Revoked user: denied before connector query.
- [ ] User requesting another College: denied by API/PDP/connector.
- [ ] Missing/expired/forged/wrong-issuer/wrong-audience token: denied without data.
- [ ] Browser-supplied elevated role/capability header: ignored; cannot self-elevate.
- [ ] Wrong service token, wrong source ID, unsupported tool, invalid contract version, extra body field, and over-limit response: safely rejected.
- [ ] Cerbos timeout, malformed response, stale policy, or unavailable PDP: fail closed.

### Data and evidence checks

- [ ] Each positive response has the requested/effective scope, source ID, retrieval time, reporting period, completeness, row count, and redaction markers.
- [ ] Cross-college, cross-department, and cross-batch isolation tests return no unauthorized rows.
- [ ] No raw identity fields, student names, emails, phones, addresses, tokens, prompts, completions, or raw audio appear in response, logs, traces, or audit records.
- [ ] Source-health degradation becomes a visible warning rather than fabricated freshness.
- [ ] Request IDs correlate API, PDP, connector, audit, and trace records without exposing sensitive payloads.
- [ ] Retention, pruning, backup, and restore checks are evidenced.

## 8. Approve and execute a limited production canary

Do not expose the connector to the full college population on the first release.

- [ ] Canary population, roles, College/Department/Batch scope, and approved test accounts are written down.
- [ ] Institution data owner, security reviewer, release owner, and rollback owner approve the start time.
- [ ] A pre-canary backup/snapshot and restore path are confirmed.
- [ ] Monitoring and alert routes are live for API, connector, database, Cerbos, identity, latency, errors, rate limits, audit, and unauthorized-denial spikes.
- [ ] Run one controlled, approved request for each enabled semantic tool.
- [ ] Verify source ID, effective scope, reporting period, provenance, redaction, latency, and audit record for each request.
- [ ] Confirm no write path, arbitrary URL, raw identity response, or cross-college result is possible.
- [ ] Hold the canary for the agreed window and compare results against exit criteria.

### Canary exit criteria

- [ ] No security or scope-isolation failure.
- [ ] No unexplained data discrepancy or freshness violation.
- [ ] Error/latency/rate-limit levels remain within the approved budget.
- [ ] No secret or sensitive payload appears in logs/traces/tickets.
- [ ] On-call can correlate and explain a request using redacted evidence.
- [ ] Institution owner and security/release owners sign the canary result.

## 9. Go-live and first-day watch

- [ ] Final configuration diff is reviewed against the approved release record.
- [ ] The deployment is made from the recorded image digests and migration revision.
- [ ] API and connector readiness checks pass after deployment.
- [ ] The first real request is performed by an approved test subject and independently reviewed.
- [ ] Source-health, provenance, effective-scope, and audit evidence are captured.
- [ ] Support/on-call has the runbook, escalation contacts, dashboards, and rollback command/change ticket.
- [ ] First-day watch owner and review checkpoints are scheduled.
- [ ] Any anomaly pauses further rollout; do not broaden the College/Department/Batch scope to work around a failure.

## 10. Rollback, disablement, and offboarding

Rollback must be executable without deleting history or erasing evidence.

- [ ] Disable or remove the first-college registry entry through the approved configuration path.
- [ ] Revoke the API-to-connector token and connector/database credentials as appropriate.
- [ ] Block the connector route or scale the service to zero if the incident requires immediate isolation.
- [ ] Roll back the API image/configuration to the last approved revision if the failure is central.
- [ ] Confirm the API no longer routes requests to the college source.
- [ ] Confirm no new connector queries occur after disablement.
- [ ] Preserve redacted audit, source-health, deployment, and incident evidence according to retention policy.
- [ ] Archive linked records; never hard-delete audit/history merely to make the source disappear.
- [ ] Record root cause, user/data impact assessment, credential rotation, and conditions for re-onboarding.

## 11. Evidence register and sign-off

For each gate, attach only redacted evidence and a link or ticket ID.

| Gate | Owner | Evidence link/ticket | Result | Date/signature |
|---|---|---|---|---|
| Contract and data mapping | Institution data owner |  | Pending |  |
| Connector/network deployment | Connector/platform owner |  | Pending |  |
| Identity and scope mapping | Identity/security owner |  | Pending |  |
| Database least privilege | Data/platform owner |  | Pending |  |
| CI/artifact/supply chain | Release owner |  | Pending |  |
| Integration/staging security tests | Security owner |  | Pending |  |
| Canary approval and result | Release + institution owner |  | Pending |  |
| Production go-live | Release/SRE owner |  | Pending |  |
| Rollback readiness | Rollback owner |  | Pending |  |

### Voice conversation and internet search

- [ ] The institution has approved, in its privacy notice, that the browser vendor (Google for Chrome, Microsoft for Edge, Apple for Safari) turns microphone audio into text, and that only the text reaches Guru Ji.
- [ ] The institution has approved that open-web search queries (never personal data; the assistant refuses those) are sent to Tavily, and has recorded `GURU_WEB_SEARCHES_PER_PERSON_PER_DAY`.
- [ ] If spoken replies use Amazon Polly (`GURU_VOICE_TTS_PROVIDER=polly`): the ECS task role has `polly:SynthesizeSpeech` in ap-south-1, and one spoken reply with Kajal was checked in English and in Hindi. Kannada replies are spoken by the device's own voice or shown as text.
- [ ] The identity provider either sends no explicit `guru_capabilities` claim, or its claim includes `web:search` for the roles that may search the internet.
- [ ] A staging check on Chrome (desktop and Android) and Safari (iPhone): talk over a reply, say "stop", ask in Hindi, ask for an internet search, and confirm a record change still waits for the on-screen Confirm.
- [ ] `GURU_VOICE_MAX_ACTIVE_SESSIONS`, `GURU_VOICE_IDLE_TIMEOUT_SECONDS` and the conversation model timeout are sized for the canary's expected concurrent users.

### Final production gate

- [ ] Every required gate above is `Pass` or has a documented, approved risk acceptance.
- [ ] No real credential is stored in the repository or checklist.
- [ ] No real college has been marked connected until the endpoint, credential, identity, reporting views, scope allowlist, and evidence are verified in the approved environment.
- [ ] The release owner records the final decision: `GO` / `NO-GO`.

## References in this repository

- `docs/EXTERNAL_VALIDATION_PLAN.md` — executable G0–G8 validation sequence.
- `docs/IMPLEMENTATION_STATUS.md` — implementation status and remaining external production gates.
- `apps/api/app/config/institution_connectors.py` — registry schema and secret-reference behavior.
- `apps/api/app/config/settings.py` — production configuration guards.
- `apps/connector/app/main.py` — connector authentication, scope, consent, limits, and endpoints.
- `apps/connector/app/repository.py` — read-only reporting repository boundary.
