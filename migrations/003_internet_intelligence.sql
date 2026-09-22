-- Guru Ji internet intelligence schema 003_internet_intelligence
-- Generated from app.internet_intelligence.store; edit the store, not this file.
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS institution_profiles ( institution_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL );
CREATE TABLE IF NOT EXISTS internet_documents ( document_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, canonical_url TEXT NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL, source_type TEXT NOT NULL, title TEXT NOT NULL, excerpt TEXT NOT NULL, content_sha256 TEXT NOT NULL, published_at TEXT, date_status TEXT NOT NULL DEFAULT 'unknown', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, retrieved_at TEXT NOT NULL, match_level TEXT NOT NULL, match_score REAL NOT NULL, match_reasons_json TEXT NOT NULL DEFAULT '[]', relevance_score REAL NOT NULL DEFAULT 0, topics_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, extracted INTEGER NOT NULL DEFAULT 0, warnings_json TEXT NOT NULL DEFAULT '[]', UNIQUE(institution_id, canonical_url) );
CREATE INDEX IF NOT EXISTS idx_internet_documents_institution ON internet_documents(institution_id, last_seen_at);
CREATE TABLE IF NOT EXISTS monitoring_runs ( run_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, queries_json TEXT NOT NULL DEFAULT '[]', new_count INTEGER NOT NULL DEFAULT 0, changed_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0, excluded_count INTEGER NOT NULL DEFAULT 0, error TEXT );
CREATE INDEX IF NOT EXISTS idx_monitoring_runs_institution ON monitoring_runs(institution_id, started_at);
CREATE TABLE IF NOT EXISTS monitoring_events ( event_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, run_id TEXT NOT NULL, document_id TEXT NOT NULL, event_type TEXT NOT NULL, importance TEXT NOT NULL, source_type TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL, alerted INTEGER NOT NULL DEFAULT 0 );
CREATE INDEX IF NOT EXISTS idx_monitoring_events_institution ON monitoring_events(institution_id, created_at);
CREATE TABLE IF NOT EXISTS intelligence_reports ( report_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, requested_by TEXT NOT NULL, question TEXT, window_days INTEGER NOT NULL, findings_count INTEGER NOT NULL, summary TEXT NOT NULL, findings_json TEXT NOT NULL, created_at TEXT NOT NULL );
CREATE INDEX IF NOT EXISTS idx_intelligence_reports_institution ON intelligence_reports(institution_id, created_at);
ALTER TABLE internet_documents ADD COLUMN IF NOT EXISTS status_reason TEXT;
ALTER TABLE internet_documents ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE internet_documents ADD COLUMN IF NOT EXISTS queries_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE internet_documents ADD COLUMN IF NOT EXISTS requested_url TEXT;
ALTER TABLE internet_documents ADD COLUMN IF NOT EXISTS last_run_id TEXT;
ALTER TABLE monitoring_runs ADD COLUMN IF NOT EXISTS stop_reason TEXT;
-- PostgreSQL row-level security (skipped on SQLite); institution_profiles stays outside it for the scheduler
ALTER TABLE internet_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE internet_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY internet_documents_tenant_isolation ON internet_documents USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE monitoring_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY monitoring_runs_tenant_isolation ON monitoring_runs USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE monitoring_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_events FORCE ROW LEVEL SECURITY;
CREATE POLICY monitoring_events_tenant_isolation ON monitoring_events USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intelligence_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE intelligence_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY intelligence_reports_tenant_isolation ON intelligence_reports USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
