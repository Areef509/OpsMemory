-- Role strategy for audit integrity. NOT APPLIED. Today docker-compose connects the backend as the `postgres`
-- SUPERUSER, which bypasses every GRANT/REVOKE and can drop the immutability triggers. To make the protection real:
--   1. Run this as a superuser on a DISPOSABLE database first.
--   2. Create the app role with a NEW random password, update DATABASE_URL and the DB role password together
--      (never one side only), restart the backend, verify new connections work and the old `postgres` URL is no longer used by the app.
CREATE ROLE opsmemory_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE opsmemory TO opsmemory_app;
GRANT USAGE ON SCHEMA public TO opsmemory_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON events, incidents, approvals TO opsmemory_app;
GRANT SELECT, INSERT ON audit_log TO opsmemory_app;          -- no UPDATE / DELETE / TRUNCATE on audit
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO opsmemory_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM opsmemory_app;
-- Separate read-only role for the verifier:
-- CREATE ROLE opsmemory_audit_reader LOGIN PASSWORD ... ; GRANT SELECT ON audit_log TO opsmemory_audit_reader;
