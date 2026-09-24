# Agentic Saffron deployment-specific external validation plan

Status: prepared for execution; no deployment, production write, migration, credential creation, or external-system change was performed while preparing this plan.

This plan converts the remaining external gates in `IMPLEMENTATION_STATUS.md` into a staged validation run. It is written for a review branch and must be completed against an approved staging environment before any production canary.

## 1. Validation principles

1. Use synthetic users and synthetic institutional aggregates in staging. Do not use real student records in test payloads, logs, screenshots, tickets, or chat.
2. Store credentials only in the approved deployment secret manager. Do not place secrets in `.env` files, CI logs, test fixtures, curl history, or this document.
3. Validate the complete trust chain: browser/API identity -> Agentic Saffron authorization -> Cerbos decision -> connector scope enforcement -> approved reporting view -> redacted response.
4. A dependency outage must deny or degrade safely. It must never widen scope, enable a client-supplied role, expose raw identity fields, or silently switch production to deterministic demo data.
5. Capture request IDs and version/image digests for every test. Record only redacted evidence.
6. A failed security test blocks progression even if functional tests pass.

## 2. Environments and promotion path

| Environment | Purpose | Data | Required external services | Promotion rule |
|---|---|---|---|---|
| Local reference | Repeatable developer checks | Deterministic demo data only | None; SQLite/local PDP allowed | Must remain green before packaging |
| Integration | Contract and dependency integration | Synthetic institution data | PostgreSQL, Cerbos, connector, test OIDC, model gateway stub or approved test model | All G0-G7 gates pass |
| Staging | Deployment-like proof | Synthetic or institution-approved masked data | Production-shaped OIDC, PostgreSQL, Cerbos, connector, model gateway, telemetry, selected optional adapters | Security, data-owner, and SRE sign-off |
| Production canary | Limited operational proof | Real data under approved access policy | All production dependencies | Canary exit criteria and rollback owner confirmed |
| Production steady state | Normal service | Real data | Fully managed and monitored stack | Formal release approval |

Production is not considered validated merely because the local suite passes.

## 3. Required inputs before starting G0

The release owner should complete this inventory without placing secret values in the checklist.

### Application/API

- `SAFFRON_ENVIRONMENT=production` only in the production deployment.
- `CONTROL_DATABASE_URL`: PostgreSQL URL supplied by the secret manager.
- `SAFFRON_ALLOWED_ORIGINS`: exact approved browser origins; no wildcard.
- `SAFFRON_OIDC_ISSUER_URL`, `SAFFRON_OIDC_AUDIENCE`, `SAFFRON_OIDC_JWKS_URL`, and approved signing algorithms.
- `SAFFRON_MODEL_PROVIDER`: `litellm` or approved non-deterministic provider; never `deterministic` in production.
- `SAFFRON_LITELLM_MODEL_ID`, gateway URL if used, and server-side API-key reference.
- `SAFFRON_ENABLE_DEMO_DATA=false`.
- `SAFFRON_PDP_MODE=cerbos`, HTTPS Cerbos URL, policy version `guru-cerbos-v1`, and the approved freshness setting.
- `SAFFRON_INSTITUTION_CONNECTOR_BASE_URL` over HTTPS, source/institution IDs, connector token reference, and `SAFFRON_CONNECTOR_SCOPE_ATTESTATION_REQUIRED=true`.
- `SAFFRON_AUDIT_FAIL_CLOSED=true`, retention period, and approved trace collector endpoint if enabled.
- Approved request, model, connector, response-size, and rate-limit budgets.

### Connector/database

- `SAFFRON_CONNECTOR_ENVIRONMENT=production`.
- A non-default `SAFFRON_CONNECTOR_SERVICE_TOKEN` held by the secret manager.
- `SAFFRON_CONNECTOR_DATABASE_URL` for the institution reporting database.
- Exact institution-approved read-only views matching the current adapter contract:
  - `public.guru_student_overview`
  - `public.guru_attendance_summary`
- Confirmed column contract for aggregate fields used by `PostgresReportingRepository`.
- A database role with `SELECT` only on the approved reporting views and no access to raw student tables, identity tables, or write operations.
- Network path, TLS certificate/CA policy, connection limits, and owner for the institution connector.

If the institution uses a different schema or view name, do not silently change the deployment value: update and review the adapter contract first, then rerun repository tests and this plan.

### Identity, policy, and operations

- Synthetic test subjects for: main admin, scoped faculty, consented student, non-consented student, revoked user, out-of-scope user, expired token, wrong-audience token, and invalid-signature token.
- OIDC claim mapping approved for role, capabilities, College/Department/Batch scopes, consent, and revocation.
- Cerbos deployment owner and policy promotion process.
- Trace/log destination, redaction review owner, alert routes, on-call owner, rollback owner, and incident channel.
- Approved RTO/RPO, latency/error SLOs, concurrency target, and maintenance window.

## 4. G0 — release candidate and supply-chain validation

Owner: release/platform engineer. Evidence: commit SHA, image digests, dependency lockfile, scan report, and CI run URL.

1. Build from the exact reviewed commit; do not build from a mutable working tree.
2. Run the repository gates:

```text
uv sync --extra dev
uv run pytest -q
uv run mypy apps/api apps/connector
uv run python -m compileall -q apps/api apps/connector tests scripts
PYTHONPATH=apps/api;apps uv run python scripts/validate_openapi.py
uv run ruff check --select F apps/api apps/connector tests scripts
uv run ruff format --check scripts/validate_openapi.py scripts/repository_hygiene.py
uv run python scripts/repository_hygiene.py
```

3. Build API and connector images in CI or a host with a working Docker daemon.
4. Generate an SBOM and vulnerability scan. Block on critical findings and review high findings before promotion.
5. Record immutable image digests, base-image versions, Python/package lockfile hash, and deployment manifest revision.
6. Verify no `.env` file, database file, test credential, prompt/completion, or generated cache is included in the image or artifact.

Pass condition: the exact artifact is reproducible, scanned, and traceable to the reviewed PR head.

## 5. G1 — infrastructure and service bootstrap in integration

Owner: platform/SRE. Evidence: deployment event, health responses, service logs with secrets redacted, and dependency version records.

Deploy only synthetic data and non-production secrets. Bring up PostgreSQL, Cerbos, the connector, the API, and the test model gateway in that order.

Check each endpoint:

```text
GET <API>/v1/health/live       -> HTTP 200, status=ok
GET <API>/v1/health/ready      -> HTTP 200, status=ready, database_ok=true, database=postgresql
GET <CONNECTOR>/v1/health     -> HTTP 200, status=healthy
GET <CONNECTOR>/v1/ready      -> HTTP 200, ready=true
GET <CERBOS>/_cerbos/health   -> healthy response
```

Then verify:

- API readiness does not report SQLite or demo data.
- Connector readiness is backed by the configured reporting database, not `DemoReportingRepository`.
- API-to-connector network access works only over the approved private path.
- Cerbos policy file/version is the approved `guru-cerbos-v1` revision.
- Startup logs contain no secret values, JWTs, database passwords, or raw student data.
- Restarting each service produces a safe readiness transition and does not create duplicate outbox effects.

Stop condition: any service reports ready while its required dependency is unavailable, or any production-shaped deployment falls back to a demo/local authority.

## 6. G2 — PostgreSQL schema, migration, and least privilege

Owner: data/platform engineer plus institution data owner. Evidence: migration output, `POSTGRES_SMOKE_OK`, grants report, backup/restore record, and query audit sample.

1. Take a backup/snapshot according to the environment policy before migration.
2. Apply the reviewed migrations using the migration identity; application and connector identities must not own the schema.
3. Run the repository smoke test:

```text
CONTROL_DATABASE_URL=<secret-manager-reference-resolved-at-runtime> \
PYTHONPATH=apps/api;apps \
python scripts/postgres_smoke.py
```

Expected output: `POSTGRES_SMOKE_OK`.

4. Using a disposable transaction and the connector role, verify:

- `SELECT` on the two approved reporting views succeeds.
- `SELECT` on raw student/identity tables fails or is not granted.
- `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `CREATE`, and `DROP` fail.
- Cross-college, cross-department, and cross-batch predicates cannot return rows outside the requested scope.
- The role cannot bypass row limits or use arbitrary identifiers.

5. Confirm the application store can persist and retrieve audit/source-health/briefing metadata, answer-envelope hashes, model-attempt metadata, and outbox records without storing answer text, prompts, completions, tokens, or raw audio.
6. Test retention pruning on disposable records and verify the approved one-year/restricted retention policy where applicable.
7. Restore the backup into an isolated database and repeat readiness plus `postgres_smoke.py`. Record measured RTO/RPO against the approved targets.

Pass condition: migrations are reversible by the approved recovery procedure, the connector is read-only, and backup restore is demonstrated rather than assumed.

## 7. G3 — OIDC/JWKS, claims, consent, and revocation

Owner: identity/security owner. Evidence: redacted token metadata, issuer/JWKS version, request IDs, expected/actual decision table, and alert records.

Use real test tokens issued by the staging IdP. Do not hand-copy signed production tokens into tickets.

| Test subject/request | Expected result |
|---|---|
| Valid main-admin token, approved College scope | Allowed only for granted capability and scope |
| Valid faculty token, approved Department/Batch scope | Allowed only within that scope |
| Valid student token with verified consent and batch scope | Allowed for permitted read-only action |
| Student token without verified consent for batch-scoped request | Denied; no connector query |
| Token with revoked/account-revoked claim or revocation-source result | Denied; no connector query |
| Valid subject requesting another College | Denied by API/PDP/connector |
| Missing `sub`, expired token, invalid signature, wrong issuer, wrong audience, unsupported algorithm | Denied; no application authority |
| Valid token with client-supplied elevated role/capability header | Header ignored; token-derived authority remains unchanged |
| Unauthenticated request | Denied or safe anonymous response according to route contract; never data |

Also validate:

- JWKS key rotation and cache refresh.
- Issuer/audience mismatch handling.
- Clock-skew behavior around `exp`/`iat`.
- Revocation propagation time against the approved security target.
- Audit event contains the request ID, principal identifier hash/approved identifier, decision outcome, and denial reason without token material.

Automatic stop: any revoked, out-of-scope, non-consented, forged, or browser-header-elevated subject receives institution data.

## 8. G4 — Cerbos policy and fail-closed behavior

Owner: policy/security owner. Evidence: deployed policy digest/version, Cerbos health, check request/response IDs, and denial tests.

1. Confirm the deployed policy version is exactly `guru-cerbos-v1` and the sidecar is the approved pinned image.
2. Exercise allowed actions: `retrieve`, `list`, `search`, and `mcp.tool.call` with valid role, capability, consent, and scope.
3. Exercise denials: unknown action, missing `ask:read_only`, revoked principal, missing scope, scope mismatch, missing student consent, malformed response, stale policy, and Cerbos timeout.
4. Stop Cerbos or make the policy endpoint unavailable in an integration window. Verify API requests do not return institutional data and that the alert fires.
5. Return a mismatched policy version and verify the API denies rather than trusting an allow bit.
6. Verify a standard Cerbos response without an echoed policy version is accepted only under the explicitly approved non-fresh compatibility setting; production must document why that setting is safe or enable attested freshness.

Pass condition: no policy transport, parsing, freshness, or policy-version failure results in an allow.

## 9. G5 — connector contract and data minimization

Owner: institution integration owner plus security owner. Evidence: contract test output, redacted response envelopes, database query audit, and negative-test results.

Use the connector directly and through the API.

Direct connector checks:

```text
GET /v1/health
GET /v1/ready
POST /v1/execute with a valid contract_version=2 request
```

Negative cases must return denial/unavailable responses as appropriate:

- missing or incorrect service bearer token;
- wrong `source_id`;
- unsupported tool name or arbitrary tool URL;
- missing principal ID/type;
- revoked principal;
- missing `ask:read_only` capability;
- student batch request without verified consent;
- requested College/Department/Batch outside connector allowlist;
- principal scope not covering the requested scope;
- invalid contract version or extra body fields;
- response over the caller's maximum bytes.

Positive cases must verify:

- `contract_version=2`;
- `tool_name` matches the request;
- effective scope is no broader than the requested scope;
- provenance contains source ID, retrieval time, reporting period, completeness, row count, and redaction markers;
- only aggregate approved columns appear;
- no raw student name, email, phone, address, token, or identity record appears;
- request ID is propagated end-to-end;
- partial/stale/refused results remain visible and are not silently converted to success.

For the API path, assert that the API rejects an invalid scope attestation before presenting data to the caller.

## 10. G6 — model gateway and answer safety

Owner: AI/platform owner. Evidence: provider configuration, model-gateway request IDs, evaluation report, cost report, and redacted trace sample.

1. Configure the approved LiteLLM/model gateway through the secret manager. Do not expose the provider key to the browser or connector.
2. Confirm production startup rejects `SAFFRON_MODEL_PROVIDER=deterministic`.
3. Run a synthetic evaluation set covering:

- correct institution and reporting-period grounding;
- citation/provenance presence;
- partial and unavailable source handling;
- out-of-scope request refusal;
- student consent refusal;
- prompt-injection/untrusted-source handling;
- timeout, rate-limit, malformed-provider-response, and provider-unavailable behavior.

4. Verify model-attempt metadata records provider/model, outcome, latency, fallback, usage, and cost fields without prompt/completion text.
5. Verify answer-envelope persistence stores only counts and a SHA-256 correlation hash, not answer text.
6. Verify model gateway failure does not cause unauthorized fallback or synthetic institution data in production.
7. Set and test per-request token, latency, concurrency, and cost limits. Compare measured values with the approved budget.

Pass condition: the model is an explanation layer over authorized evidence, never the authority for identity, scope, consent, or revocation.

## 11. G7 — observability, audit, and operations

Owner: SRE/security. Evidence: trace/log samples, alert screenshots or event IDs, outbox retry record, retention result, and runbook links.

Validate:

- request ID is present in API, connector, PDP, audit, and trace records;
- logs and traces redact authorization headers, database URLs/passwords, raw prompts, completions, student identifiers, and audio;
- audit failure follows the configured production fail-closed policy;
- outbox retry delivers at least once and acknowledges only after successful handling;
- source-health degradation is visible and causes honest answer warnings;
- alerts fire for API not-ready, connector unavailable, Cerbos unavailable/stale, database failure, provider failure, latency budget breach, audit failure, and repeated authorization denials;
- on-call can identify a request without reconstructing sensitive payloads;
- retention and deletion jobs have a recorded result and do not delete required audit evidence prematurely.

Conduct one tabletop exercise for: connector outage, Cerbos outage, OIDC key rotation failure, database restore, and suspected unauthorized access.

## 12. G8 — optional integration adapters

Run only the adapters approved for this deployment. Each adapter must have a named owner and a kill switch.

### Open edX/Moodle edge identity

- Valid server-authenticated edge response maps only approved roles/scopes/consent/revocation.
- Invalid signature/token, timeout, malformed response, or unavailable edge service fails closed.
- Browser-supplied role/capability headers cannot elevate the principal.
- Cross-institution and cross-batch requests are denied.

### OpenFGA

- Approved relationship allows the intended scoped read.
- Missing relationship, unavailable store, malformed model, and stale model deny.
- Relationship checks cannot expand the institution scope supplied by the verified principal.

### MCP/agent gateway

- Only explicitly allowlisted gateway target and tool succeed.
- Arbitrary client-supplied URLs, tools, or headers are rejected.
- Gateway credentials remain server-side.
- Gateway failure never bypasses normal PDP/connector authorization.

### LiveKit/Pipecat/WebRTC voice

- Authentication and session scope match the text path.
- Lifecycle and final-transcript metadata are captured as designed.
- Raw audio is not persisted by Agentic Saffron.
- Disconnect, reconnect, timeout, unauthorized room, and provider outage are safe.
- The 10-concurrent-session cap is enforced or enforced by the approved edge layer.

## 13. G9 — performance, resilience, backup, and recovery

Owner: SRE/platform. Evidence: load report, resource graphs, failure-injection results, backup/restore report, and incident/runbook references.

Before running load, approve numerical targets for p50/p95 latency, error rate, token/cost budget, database connections, and recovery time/objectives. Do not treat an unapproved target as a pass criterion.

Minimum scenarios:

1. Ten concurrent live sessions for the approved soak period, including text and selected voice/session lifecycle traffic.
2. Burst traffic at the configured API rate limit and at the gateway's expected production rate.
3. API worker restart during an active request.
4. Connector restart and reporting-database connection loss.
5. Cerbos restart, timeout, stale policy, and policy reload.
6. Model gateway timeout, rate limit, malformed response, and outage.
7. PostgreSQL primary restart/failover according to the managed service procedure.
8. Trace collector outage and audit-store pressure.
9. Backup restore into an isolated environment followed by readiness and smoke tests.
10. Rolling deployment with no mixed-contract failure between API and connector contract version 2.

Success requires no unauthorized response, no raw-data exposure, no silent data loss beyond the approved RPO, and recovery within the approved SLOs. A performance pass cannot override a security failure.

## 14. G10 — staging exit and production canary

### Staging exit checklist

- G0-G9 evidence is attached to the release record.
- Security owner signs identity, scope, consent, revocation, Cerbos, redaction, and negative tests.
- Institution data owner signs view definitions, reporting periods, data minimization, and read-only grants.
- SRE signs readiness, alerts, load, backup/restore, rollback, and incident runbooks.
- Product owner signs answer quality, citations, warnings, and user-facing failure behavior.
- Release owner confirms image digests, config references, migration revision, and rollback target.

### Production canary procedure

1. Freeze the release artifact and configuration references.
2. Verify the production preflight without sending customer traffic.
3. Deploy API/connector/PDP with the previous release available for rollback.
4. Run health/readiness and one synthetic or approved canary request.
5. Route only the approved canary population or internal test subjects.
6. Monitor authorization denials/allows, connector scope attestations, source health, model cost/latency, audit delivery, error rate, and database load.
7. Stop and roll back immediately on any automatic stop condition below.
8. Expand traffic only after the canary observation window and all owners sign the release record.

Automatic rollback triggers:

- any unauthorized allow;
- any out-of-scope, revoked, or non-consented data returned;
- missing or broader-than-requested scope attestation;
- Cerbos, OIDC, connector, or audit failure resulting in data availability rather than denial;
- raw identity data, credentials, prompts, completions, or audio in logs/traces/storage;
- production demo data or deterministic provider activation;
- migration, backup, or rollback failure;
- approved latency/error/cost budget breach;
- inability to identify and contain a request through the operational runbook.

## 15. Evidence record template

Create one record per test case with:

```text
Test ID:
Environment:
UTC timestamp:
Release commit/image digest:
Config revision/secret reference IDs (never secret values):
Actor class/test subject pseudonym:
Request ID(s):
Preconditions:
Command or controlled action:
Expected result:
Observed result:
Security/data-handling observation:
Evidence location:
Owner:
Status: PASS / FAIL / BLOCKED
Defect or incident reference:
Reviewer/sign-off:
```

Do not attach raw JWTs, bearer tokens, database URLs, student records, raw model prompts/completions, or audio.

## 16. Final sign-off matrix

| Gate | Required approver | Exit evidence |
|---|---|---|
| G0 artifact/supply chain | Release/platform owner | Immutable artifact, SBOM, scan, repository gates |
| G1 infrastructure bootstrap | SRE/platform owner | Health/readiness and dependency evidence |
| G2 database/data boundary | Institution data owner + platform | Migration, grants, read-only, smoke, restore evidence |
| G3 identity/consent/revocation | Identity owner + security | Positive/negative token matrix and rotation evidence |
| G4 Cerbos policy | Security/policy owner | Version, allow/deny, stale/outage evidence |
| G5 connector/data minimization | Institution integration owner + security | Contract, scope, provenance, redaction, query evidence |
| G6 model gateway | AI/platform owner | Evaluation, usage/cost, failure-mode evidence |
| G7 observability/operations | SRE/security | Redacted telemetry, alerts, retry, retention, tabletop |
| G8 optional adapters | Respective service owners | Adapter-specific allow/deny and outage evidence |
| G9 resilience/DR | SRE/platform owner | Load, failure injection, restore, RTO/RPO evidence |
| G10 production canary | Release owner + all required approvers | Canary report and rollback readiness |

The production claim may be made only when all required gates for the selected deployment profile are PASS. Gates for intentionally disabled optional adapters should be recorded as `NOT APPLICABLE` with an owner-approved rationale, never silently omitted.
