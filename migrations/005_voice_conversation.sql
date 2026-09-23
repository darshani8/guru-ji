-- Guru Ji control-plane schema v005: voice sessions and per-person usage counters.
-- A voice session row lets the request that opens a session and the WebSocket
-- that uses it reach different API tasks; its one-use ticket is stored only as
-- a SHA-256 digest. No transcript, answer or audio is ever stored here.
-- The API applies this at start-up (persistence/database.py); it is kept here for review.
CREATE TABLE IF NOT EXISTS voice_sessions (session_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, institution_id TEXT NOT NULL, principal_json TEXT NOT NULL, ticket_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, ticket_expires_at TEXT NOT NULL, expires_at TEXT NOT NULL, claimed_at TEXT, closed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_principal ON voice_sessions(principal_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_expires_at ON voice_sessions(expires_at);
CREATE TABLE IF NOT EXISTS usage_counters (institution_id TEXT NOT NULL, principal_id TEXT NOT NULL, counter TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (institution_id, principal_id, counter, day));
