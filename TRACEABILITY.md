# Requirement and Verification Traceability

Statuses distinguish source implementation, automated unit tests, and live container/database verification.

| Area | Implementation | Automated Verification | Live Verification Status |
|---|---|---|---|
| **Event Ingestion, Normalization, Idempotency** | `backend/app/core/events.py`, `backend/app/engine.py` | Unit tests (`test_core.py`, `test_engine.py`); deduplication tests | **VERIFIED LIVE** — tested against PostgreSQL 16 container with `test_full_workflow_postgres.py` |
| **Recursive Secret/Email Redaction** | `backend/app/core/events.py` | Nested metadata and raw payload tests (`test_runtime_hardening.py`) | **VERIFIED IN POSTGRES JSONB** — secrets and emails confirmed redacted before persistence |
| **Event Correlation and Severity** | `backend/app/core/correlation.py`, `severity.py` | Unit tests (`test_core.py`); multi-service dependency tests | **VERIFIED LIVE** — correlated multi-service payment pool exhaustion incident verified in DB |
| **Groq Investigation & Anti-Fabrication** | `integrations/groq_client.py`, `core/investigation.py` | Stub retry/auth/parse tests (`test_pipeline.py`); markdown code fence and reasoning tag stripping (`test_providers_integration.py`) | **CLIENT VERIFIED & TESTED** — live probe ready; executes against `https://api.groq.com` when key is supplied |
| **Hindsight Memory** | `integrations/hindsight_memory.py` | Memory-flow test double (`test_engine.py`); SDK contract tests with version probe, bank auto-creation, and rich recall (`test_providers_integration.py`) | **CLIENT VERIFIED & TESTED** — live probe ready; executes against Hindsight server when URL is supplied |
| **Durable PostgreSQL Store** | `backend/app/store_memory.py`, migrations 001/002 | Protocol restart tests (`test_runtime_hardening.py`) | **VERIFIED LIVE** — `PostgresStore` operating against running PostgreSQL 16 container (`opsmemory-db-1`) |
| **Human Approval and RBAC** | `core/remediation.py`, `main.py`, `engine.py` | Unit tests (`test_pipeline.py`); FastAPI role test (`test_runtime_hardening.py`) | **VERIFIED LIVE** — `viewer` denied with audit logging, `sre` approved and logged in PostgreSQL |
| **Recovery Verification** | `core/recovery.py`, `main.py` | Local Prometheus-compatible server test; caller metrics rejected; fail-closed behavior verified | **VERIFIED FAIL-CLOSED** — queries server-configured metrics source; refuses caller metrics |
| **Synthetic Evidence Isolation** | `main.py`, `core/postmortem.py` | Synthetic incidents excluded from memory retention (`test_pipeline.py`) | **VERIFIED IN DB** — synthetic flag enforced by server |
| **Post-Mortem Generation** | `core/postmortem.py` | Unit tests (`test_pipeline.py`) | **VERIFIED LIVE** — confirmed root cause and executed resolutions captured in post-mortem |
| **Dashboard** | `frontend/index.html`, `main.py` | Served by FastAPI route test (`test_runtime_hardening.py`) | **DEPLOYED LIVE** — executive SRE cockpit operational at `http://127.0.0.1:8000/` |
| **Docker Deployment** | `Dockerfile`, `docker-compose.yml` | Static Dockerfile review | **VERIFIED LIVE** — `opsmemory-backend-1` and `opsmemory-db-1` healthy and responding |
| **Safe Remediation Adapters** | `backend/app/main.py`, `remediation.py` | Allowlist enforcement tests (`test_pipeline.py`) | **IMPLEMENTED & CONFIGURED** — `restart-service`, `scale-pool`, `clear-cache`, `demo-noop` registered |
| **Hindsight Before/After Experiment** | `scripts/run_ab_experiment.py` | A/B recommendation transformation test (`test_providers_integration.py`) | **SCRIPT READY** — exits cleanly when credentials missing; ready for live run |
| **Band AI Desktop, Content & Video** | Manual team tasks | Not code-testable | **NOT DONE** — manual submission tasks |
