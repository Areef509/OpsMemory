-- 005: additive tenant ownership columns.
-- Existing records are explicitly isolated under "legacy"; never expose them
-- by silently assigning them to a newly configured tenant.
BEGIN;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'legacy';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'legacy';
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'legacy';
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'legacy';
ALTER TABLE investigation_experiments ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'legacy';

CREATE INDEX IF NOT EXISTS events_tenant_incident_idx ON events(tenant_id, incident_id);
CREATE INDEX IF NOT EXISTS incidents_tenant_created_idx ON incidents(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS approvals_tenant_incident_idx ON approvals(tenant_id, incident_id);
CREATE INDEX IF NOT EXISTS audit_log_tenant_ts_idx ON audit_log(tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS experiments_tenant_incident_idx ON investigation_experiments(tenant_id, incident_id);
COMMIT;
