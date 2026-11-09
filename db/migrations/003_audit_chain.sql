-- 003: tamper-evident audit chain. ADDITIVE (no data rewritten). NOT YET APPLIED anywhere.
-- Apply FIRST to an isolated copy/disposable database. Rollback: db/rollback/003_audit_chain.down.sql (kept outside
-- the initdb mount on purpose so it can never run automatically).
-- Legacy rows (seq IS NULL) are left untouched and are NOT covered by the chain; the chain starts at the first new row.
ALTER TABLE audit_log
  ADD COLUMN IF NOT EXISTS seq bigint,
  ADD COLUMN IF NOT EXISTS ts_signed text,
  ADD COLUMN IF NOT EXISTS prev_hash text,
  ADD COLUMN IF NOT EXISTS hash text,
  ADD COLUMN IF NOT EXISTS canonical text,
  ADD COLUMN IF NOT EXISTS resource_id text,
  ADD COLUMN IF NOT EXISTS correlation_id text;
CREATE UNIQUE INDEX IF NOT EXISTS audit_log_seq_uq ON audit_log(seq) WHERE seq IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS audit_log_hash_uq ON audit_log(hash) WHERE hash IS NOT NULL;

CREATE OR REPLACE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP USING ERRCODE = 'insufficient_privilege';
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change();
-- NOTE: a superuser/table owner can still DROP the trigger. Real protection also needs the application to connect
-- as a limited role (see docs/AUDIT_DB_ROLES.sql) and the head hash to be anchored outside the database.
