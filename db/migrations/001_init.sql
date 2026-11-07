-- Initial PostgreSQL schema. Applied on first database initialization.
CREATE TABLE IF NOT EXISTS events (
  event_id text PRIMARY KEY,            -- idempotency: duplicate inserts are rejected
  ts timestamptz NOT NULL, service text NOT NULL, environment text NOT NULL,
  severity text NOT NULL, source text NOT NULL, message text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}', synthetic boolean NOT NULL DEFAULT false,
  raw jsonb NOT NULL, incident_id text);
CREATE TABLE IF NOT EXISTS incidents (
  id text PRIMARY KEY, severity text NOT NULL, state text NOT NULL,
  services text[] NOT NULL, synthetic boolean NOT NULL, data jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS approvals (
  id text PRIMARY KEY, incident_id text NOT NULL REFERENCES incidents(id),
  action text NOT NULL, adapter text NOT NULL, state text NOT NULL, result jsonb);
CREATE TABLE IF NOT EXISTS audit_log (   -- append-only; app role should have INSERT/SELECT only
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL, event text NOT NULL, detail jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_incident ON events(incident_id);
