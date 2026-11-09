CREATE TABLE IF NOT EXISTS investigation_experiments (
    id text PRIMARY KEY,
    incident_id text NOT NULL REFERENCES incidents(id),
    actor text NOT NULL,
    evidence_class text NOT NULL,
    incident_synthetic boolean NOT NULL,
    memory_off jsonb NOT NULL,
    memory_on jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT experiment_evidence_class_check
        CHECK (evidence_class IN ('controlled_local_hackathon_test', 'production'))
);

CREATE INDEX IF NOT EXISTS investigation_experiments_incident_created
    ON investigation_experiments(incident_id, created_at DESC);

CREATE OR REPLACE FUNCTION investigation_experiments_reject_change()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'investigation_experiments is append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investigation_experiments_no_update_delete
    ON investigation_experiments;
CREATE TRIGGER investigation_experiments_no_update_delete
    BEFORE UPDATE OR DELETE ON investigation_experiments
    FOR EACH ROW EXECUTE FUNCTION investigation_experiments_reject_change();

DROP TRIGGER IF EXISTS investigation_experiments_no_truncate
    ON investigation_experiments;
CREATE TRIGGER investigation_experiments_no_truncate
    BEFORE TRUNCATE ON investigation_experiments
    FOR EACH STATEMENT EXECUTE FUNCTION investigation_experiments_reject_change();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
        EXECUTE 'GRANT SELECT, INSERT ON investigation_experiments TO opsmemory_app';
        EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON investigation_experiments FROM opsmemory_app';
    ELSE
        RAISE NOTICE 'opsmemory_app role absent; restricted-role grants deferred';
    END IF;
END
$$;
