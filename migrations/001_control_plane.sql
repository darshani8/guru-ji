-- Guru Ji control-plane schema v001
-- The runtime applies this idempotently for local SQLite.
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, request_id TEXT NOT NULL, occurred_at TEXT NOT NULL, principal_id TEXT, endpoint TEXT, conversation_id TEXT, source_ids_json TEXT NOT NULL, tool_names_json TEXT NOT NULL, outcome TEXT NOT NULL, redactions_json TEXT NOT NULL, duration_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at ON audit_events(occurred_at DESC);
CREATE TABLE IF NOT EXISTS source_health (source_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', connector_type TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL, last_success_at TEXT, latency_ms INTEGER, freshness TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS briefing_runs (briefing_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, principal_id TEXT NOT NULL, institution_id TEXT NOT NULL, answer_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_briefing_runs_created_at ON briefing_runs(created_at DESC);
