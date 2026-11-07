-- Idempotent event assignment and durable approval payloads for the runtime repository.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS data jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS incidents_created_at ON incidents(created_at DESC);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log(ts DESC);
