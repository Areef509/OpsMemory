# Security Review & Audit Findings

Review Scope: Application security audit covering authentication, authorization, SQL injection, input validation, prompt injection, secret exposure, security headers, safe remediation adapters, application audit trail, and recovery verification trust boundaries.

---

### Application Security Controls & Verification Status

1. **Authentication & Authorization (Role-Based Access Control)**:
   - Implementation: `backend/app/main.py#L40-L65`. Validates `X-API-Key` using constant-time comparison (`secrets.compare_digest`) against configured `OPSMEMORY_API_KEYS`.
   - Role Enforcement:
     - `viewer`: Read-only access (`GET /incidents`, `GET /incidents/{id}`, `GET /adapters`). All state-mutating endpoints (`/events`, `/investigate`, `/remediations`, `/decision`, `/execute`, `/verify`) are rejected with HTTP 403.
     - `sre`: Operator access. Permitted to ingest event batches, trigger AI investigation, request remediation approval, and record approval decisions.
     - `admin`: Elevated access. Required exclusively for server-side recovery verification and incident resolution (`POST /incidents/{id}/verify`).
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_viewer_cannot_ingest` (`backend/tests/unit/test_runtime_hardening.py`) and `test_viewer_cannot_approve_and_is_audited` (`backend/tests/unit/test_pipeline.py`).
     - Live Provider: VERIFIED WORKING against local FastAPI backend and PostgreSQL container.

2. **SQL Injection Prevention & Parameterized Queries**:
   - Implementation: `backend/app/store_memory.py` (`PostgresStore`). All SQL statements execute parameterized queries using `%s` placeholders with typed parameter tuples via `psycopg`.
   - Hardening: Zero dynamic SQL string concatenation, raw formatting, or f-strings in database access layer.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_complete_incident_lifecycle_with_real_postgres` (`backend/tests/integration/test_full_workflow_postgres.py`).
     - Live Provider: VERIFIED WORKING against live PostgreSQL 16 container (`opsmemory-db-1`).

3. **Input Validation, Deserialization & Boundary Protection**:
   - Implementation: Pydantic schemas in `backend/app/main.py` enforce bounds (`Field(min_length=1, max_length=500)` for event batches; `max_length=1000` for decision reasons; `max_length=64` for adapter identifiers). ISO-8601 parser rejects malformed timestamps in `backend/app/core/events.py`.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_bad_severity_and_timestamp` and `test_missing_fields_rejected` (`backend/tests/unit/test_core.py`).
     - Live Provider: VERIFIED WORKING via FastAPI schema validation.

4. **Prompt Injection & Memory Poisoning Defense**:
   - Implementation: `backend/app/core/safety.py` (`sanitize_untrusted`). Detects adversarial instructions (e.g. `IGNORE ALL PREVIOUS INSTRUCTIONS`), prefixes `[UNTRUSTED MEMORY; possible prompt injection]`, and strips non-printable control characters (`\x00`–`\x1f`).
   - Synthetic Data Isolation Guarantee: Server-controlled synthetic flags (`backend/app/main.py#L169`) and gated retention logic (`backend/app/core/postmortem.py#L6-L19`) guarantee that synthetic fixtures and unverified incidents are never promoted to production Hindsight memory.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_retrieved_memory_is_marked_and_sanitized_before_llm` (`test_runtime_hardening.py`) and `test_injection_flagged_and_control_chars_stripped` (`test_engine.py`).
     - Live Provider: IMPLEMENTED — NOT VERIFIED. Live bank isolation in Hindsight remains unverified until tested against the real Hindsight provider.

5. **Recursive Credential & Sensitive Data Redaction**:
   - Implementation: `backend/app/core/events.py`. Recursively inspects nested dictionary metadata and raw payloads for keys matching credential names (`password`, `token`, `secret`, `api_key`, `authorization`) and regex patterns for bearer tokens and email addresses.
   - Replacement: Replaces sensitive data with `[REDACTED]` and `[REDACTED_EMAIL]` prior to database persistence and prior to LLM prompt formatting.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_nested_metadata_and_raw_payload_are_redacted` (`test_runtime_hardening.py`) and `test_redaction` (`test_core.py`).
     - Live Provider: VERIFIED WORKING in PostgreSQL JSONB storage during integration test.

6. **Anti-Fabrication Investigation Grounding**:
   - Implementation: `backend/app/core/investigation.py` (`validate`). Enforces that every event ID cited in LLM causal hypotheses exists in the candidate's normalized event list. Hypotheses citing non-existent event IDs are dropped. Restricts causal status to `hypothesis_only`.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_fabricated_event_id_dropped` and `test_no_evidence_dropped` (`test_pipeline.py`).
     - Live Provider: IMPLEMENTED — NOT VERIFIED. Live grounding verification against Groq API remains unverified until executed with live `GROQ_API_KEY`.

7. **Transport Security & Security Headers**:
   - Implementation: `backend/app/main.py`. Configures `CORSMiddleware` and `SecurityHeadersMiddleware`.
   - Headers Enforced:
     - `X-Content-Type-Options: nosniff`
     - `X-Frame-Options: DENY`
     - `Referrer-Policy: strict-origin-when-cross-origin`
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_security_headers_present` (`test_runtime_hardening.py`).
     - Live Provider: VERIFIED WORKING on HTTP responses from backend server.

8. **Server-Side Recovery Metric Trust Boundary**:
   - Implementation: `backend/app/core/recovery.py` and `backend/app/main.py#L210-L217`. Recovery verification queries server-configured Prometheus metrics endpoints. Caller-supplied recovery samples in HTTP request bodies are rejected. Fails closed with HTTP 503 if metrics are unconfigured or unavailable.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_verify_ignores_caller_supplied_metrics_and_fails_closed` and `test_unconfigured_metrics_fails_closed` (`test_runtime_hardening.py`).
     - Live Provider: VERIFIED WORKING (fails closed when Prometheus is unconfigured).

9. **Safe Controlled Remediation Gating**:
   - Implementation: `backend/app/core/remediation.py` and `backend/app/engine.py`. Remediation execution is restricted strictly to registered adapter names (`e.adapters`). Double execution is blocked. Rejected requests cannot be executed.
   - Simulation Tagging: Demonstration adapters (`scale-pool`, `restart-service`, `demo-noop`) are explicitly marked `simulated: True` and do not execute destructive host operations.
   - Verification Status:
     - Automated Tests: VERIFIED WORKING via `test_cannot_execute_unapproved_or_rejected` and `test_double_decision_blocked` (`test_pipeline.py`).
     - Live Provider: VERIFIED WORKING in application lifecycle tests.

10. **Append-Only Application Audit Trail**:
    - Implementation: `backend/app/store_memory.py` (`append_audit`). Application logic records operator actions, approval decisions, rejection reasons, and adapter executions using `INSERT` statements to the `audit_log` table.
    - Note on Immutability: This is an append-only application audit trail enforced by application code. Database-level immutability (e.g. `REVOKE UPDATE, DELETE`, WORM storage, or cryptographic block hashing) has not been implemented at the database engine level.
    - Verification Status:
      - Automated Tests: VERIFIED WORKING via `test_complete_incident_lifecycle_with_real_postgres` (`test_full_workflow_postgres.py#L244-L250`).
      - Live Provider: VERIFIED WORKING in PostgreSQL 16 container.

---

### Enterprise Production Prerequisites

Before deploying OpsMemory in production environments:
1. **TLS / HTTPS Termination**: Deploy behind an enterprise reverse proxy (e.g. Nginx, Envoy, or Cloudflare) enforcing TLS 1.3 with automated certificate renewal and HSTS headers.
2. **Enterprise Identity Integration**: Replace static `OPSMEMORY_API_KEYS` with an enterprise OIDC/OAuth2/SAML identity provider supporting MFA and credential rotation.
3. **Distributed Rate Limiting**: Implement a Redis-backed token bucket on public endpoints (`/events`, `/health`, `/ready`) to protect against denial-of-service surges.
4. **Secrets Management**: Move database credentials and provider API keys from `.env` to a dedicated secrets vault (e.g. HashiCorp Vault, AWS Secrets Manager).
5. **Remediation Connector Sandboxing**: Production connectors mutating cloud hypervisors (Kubernetes API, AWS SDK) must execute in isolated sandboxes with strict least-privilege IAM policies.
6. **Database Audit Immutability**: Configure PostgreSQL permissions to `REVOKE UPDATE, DELETE ON audit_log FROM opsmemory_app` to enforce database-level immutability.

---

### Non-Fabrication & Certification Disclaimer

- Demonstration adapters (`demo-noop`, `scale-pool`, `restart-service`) simulate infrastructure actions in current test configurations and must not be mistaken for real hypervisor executions.
- Synthetic experiment fixtures are quarantined and never represented as verified production evidence.
- This review documents verified application-level security controls; no claim of zero vulnerabilities or third-party SOC2/ISO-27001 production certification is made.

## Session 5 review (manual code review; no scanner or pen-test run)
Fixed: recovery could be "verified" from OpsMemory's own API metrics for an unrelated service (High: false-recovery + false verified memory); simulated adapters could count as real remediation in postmortems (High); non-ASCII API key caused HTTP 500; no rate limiting; provider diagnostics callable by viewers (triggers live paid calls); no CSP/no-store; demo button inserted hard-coded events as "production" incidents (Medium); possible double execution under concurrency (Medium); A/B script could continue after fixture/recall failure and exit 0.
Regression tests: `test_session5_gates.py` (core, executed), `test_api_session5.py` (API; requires FastAPI, skipped in the authoring sandbox).
Unresolved: no tenant isolation (one Hindsight bank, RBAC only); static API keys; limiter is per-process and IP-keyed; claim lock is per-process (multi-replica needs DB compare-and-set); no dependency or container scan; audit log not tamper-evident; `/ready` creates a store per call; integration tests insert rows into the live DB (run them against a separate database).
