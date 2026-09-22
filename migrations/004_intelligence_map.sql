-- Guru Ji internet map schema 004_intelligence_map
-- Generated from app.internet_intelligence.map.store; edit the store, not this file.
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intel_entities ( entity_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, names_json TEXT NOT NULL DEFAULT '[]', locations_json TEXT NOT NULL DEFAULT '[]', group_label TEXT NOT NULL DEFAULT '', parent_id TEXT, authority TEXT NOT NULL DEFAULT 'self', status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(institution_id, kind, name) );
CREATE INDEX IF NOT EXISTS idx_intel_entities_institution ON intel_entities(institution_id, kind);
CREATE TABLE IF NOT EXISTS intel_assets ( asset_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, entity_id TEXT, asset_key TEXT NOT NULL, platform TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, handle TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'unknown', grade TEXT NOT NULL DEFAULT 'unrated', grade_reasons_json TEXT NOT NULL DEFAULT '[]', proposed_grade TEXT, scorer_version TEXT, status TEXT NOT NULL DEFAULT 'unknown', platform_id TEXT, followers INTEGER, followers_source TEXT, followers_at TEXT, last_verified_at TEXT, last_verified_via TEXT, note TEXT NOT NULL DEFAULT '', first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(institution_id, asset_key) );
CREATE INDEX IF NOT EXISTS idx_intel_assets_institution ON intel_assets(institution_id, platform, grade);
CREATE TABLE IF NOT EXISTS intel_evidence ( evidence_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, asset_id TEXT NOT NULL, kind TEXT NOT NULL, polarity TEXT NOT NULL DEFAULT 'supports', source_url TEXT NOT NULL DEFAULT '', source_asset_id TEXT, detail TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '', observed_via TEXT NOT NULL DEFAULT 'live', run_id TEXT, raw_sha256 TEXT, observed_at TEXT NOT NULL );
CREATE INDEX IF NOT EXISTS idx_intel_evidence_asset ON intel_evidence(institution_id, asset_id, observed_at);
CREATE TABLE IF NOT EXISTS intel_gold_items ( gold_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, asset_key TEXT NOT NULL, platform TEXT NOT NULL, entity_name TEXT NOT NULL DEFAULT '', relation TEXT NOT NULL DEFAULT 'unknown', split TEXT NOT NULL, expected_min_grade TEXT NOT NULL DEFAULT 'C', source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(institution_id, asset_key) );
CREATE TABLE IF NOT EXISTS intel_suppression ( institution_id TEXT NOT NULL, key_hmac TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(institution_id, key_hmac) );
CREATE TABLE IF NOT EXISTS intel_map_runs ( run_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'tick', started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, stop_reason TEXT, gate TEXT, spend_json TEXT NOT NULL DEFAULT '{}', counts_json TEXT NOT NULL DEFAULT '{}', metrics_json TEXT NOT NULL DEFAULT '{}', error TEXT );
CREATE INDEX IF NOT EXISTS idx_intel_map_runs_institution ON intel_map_runs(institution_id, started_at);
-- PostgreSQL row-level security (skipped on SQLite)
ALTER TABLE intel_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_entities FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_entities_tenant_isolation ON intel_entities USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intel_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_assets FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_assets_tenant_isolation ON intel_assets USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intel_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_evidence_tenant_isolation ON intel_evidence USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intel_gold_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_gold_items FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_gold_items_tenant_isolation ON intel_gold_items USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intel_suppression ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_suppression FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_suppression_tenant_isolation ON intel_suppression USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
ALTER TABLE intel_map_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_map_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY intel_map_runs_tenant_isolation ON intel_map_runs USING (institution_id = current_setting('app.institution_id', true)) WITH CHECK (institution_id = current_setting('app.institution_id', true));
