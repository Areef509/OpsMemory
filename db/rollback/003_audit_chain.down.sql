-- Manual rollback of 003 (never mounted into docker-entrypoint-initdb.d). Drops chain metadata only; legacy columns remain.
DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
DROP FUNCTION IF EXISTS audit_log_reject_change();
DROP INDEX IF EXISTS audit_log_seq_uq;
DROP INDEX IF EXISTS audit_log_hash_uq;
ALTER TABLE audit_log DROP COLUMN IF EXISTS seq, DROP COLUMN IF EXISTS ts_signed, DROP COLUMN IF EXISTS prev_hash,
  DROP COLUMN IF EXISTS hash, DROP COLUMN IF EXISTS canonical, DROP COLUMN IF EXISTS resource_id, DROP COLUMN IF EXISTS correlation_id;
