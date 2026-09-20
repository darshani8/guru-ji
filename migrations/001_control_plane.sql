-- Guru Ji control-plane schema v001
-- Institutional data remains behind read-only connector boundaries.
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, request_id TEXT NOT NULL, occurred_at TEXT NOT NULL, principal_id TEXT, endpoint TEXT, conversation_id TEXT, source_ids_json TEXT NOT NULL, tool_names_json TEXT NOT NULL, outcome TEXT NOT NULL, redactions_json TEXT NOT NULL, decision_metadata_json TEXT NOT NULL DEFAULT '{}', duration_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at ON audit_events(occurred_at DESC);
CREATE TABLE IF NOT EXISTS source_health (source_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', connector_type TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL, last_success_at TEXT, latency_ms INTEGER, freshness TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS briefing_runs (briefing_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, principal_id TEXT NOT NULL, institution_id TEXT NOT NULL, answer_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_briefing_runs_created_at ON briefing_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS answer_envelopes (request_id TEXT PRIMARY KEY, conversation_id TEXT, principal_id TEXT, college_id TEXT, status TEXT NOT NULL, citations_count INTEGER NOT NULL, warnings_count INTEGER NOT NULL, answer_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_answer_envelopes_created_at ON answer_envelopes(created_at DESC);
CREATE TABLE IF NOT EXISTS model_attempts (attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider_id TEXT NOT NULL, model_id TEXT NOT NULL, outcome TEXT NOT NULL, latency_ms INTEGER NOT NULL, fallback INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cost REAL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_model_attempts_created_at ON model_attempts(created_at DESC);
CREATE TABLE IF NOT EXISTS control_outbox (outbox_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
CREATE INDEX IF NOT EXISTS idx_control_outbox_pending ON control_outbox(delivered_at, created_at);
