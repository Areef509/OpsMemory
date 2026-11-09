# OpsMemory — Autonomous Incident Response & Continuous Memory

OpsMemory is an AI-assisted incident response agent built with FastAPI, PostgreSQL, Groq, Hindsight, and Prometheus. It ingests operational events, correlates symptoms, assigns explainable severity, and investigates incidents with evidence-grounded LLM hypotheses. It includes persistent semantic memory integration, role-based approval controls, and server-side monitoring collection. The live demo verifies a real Groq investigation persisted to PostgreSQL. Verified scope: tenant isolation passed live cross-tenant tests against disposable PostgreSQL; the full incident-to-postmortem workflow passed its PostgreSQL integration test; and a live synthetic-fixture A/B experiment was executed. Limitations: production remediation remains unauthorized, payment-gateway production recovery is unverified, and relevant historical incident learning is not proven.

## System Architecture

- **API & Orchestrator:** FastAPI & Engine (`backend/app/main.py`, `backend/app/engine.py`)
- **Durable State:** PostgreSQL 16 with JSONB payload storage, indexed audit trail, and schema migrations (`backend/app/store_memory.py`, `db/migrations/`)
- **Causal Reasoning:** Groq chat-completions client (`backend/app/integrations/groq_client.py`) with anti-fabrication evidence grounding, exponential backoff, rate-limit `Retry-After` handling, and markdown normalization
- **Continuous Memory Bank:** Hindsight Python SDK 1.2.0 (`backend/app/integrations/hindsight_memory.py`) with semantic recall, automatic bank profile initialization (`retain_mission` / `reflect_mission`), and gated retention
- **Recovery Telemetry:** Prometheus-compatible `query_range` collector (`backend/app/core/recovery.py`) enforcing consecutive sample thresholds
- **SRE Cockpit Dashboard:** High-density, real-time dark-mode SRE dashboard (`frontend/index.html`) with live provider health probes, interactive incident workspace, and human-in-the-loop approval controls
- **Container Infrastructure:** Docker Compose (`docker-compose.yml`)

## Safety & Operational Invariants

1. **Anti-Fabrication Validation:** Every hypothesis emitted by the LLM must cite valid event IDs present in the real incident candidate. Any hypothesis citing fabricated or unknown IDs is dropped.
2. **Untrusted Data Boundary:** Retrieved memories and raw event messages are treated as untrusted data. Control characters are stripped and potential prompt injection patterns are quarantined before LLM processing.
3. **Recursive Redaction:** Secrets, passwords, API tokens, and email addresses are recursively redacted in nested metadata and raw event payloads prior to persistence and LLM prompting.
4. **Idempotency & Transactional Persistence:** Ingestion deduplicates events using deterministic content hashes and PostgreSQL unique constraints. Duplicate event batches are handled idempotently.
5. **Role-Based Access Control (RBAC):**
   - `viewer`: Read-only access to incidents and diagnostics. Remediation requests and approvals are forbidden (HTTP 403) and logged to the immutable `audit_log`.
   - `sre`: Can ingest events, trigger investigations, request remediation, and decide/execute allow-listed actions.
   - `admin`: Full operator access plus authority to trigger recovery verification (`POST /incidents/{id}/verify`).
6. **Remediation safety:** The engine supports approval-gated adapter execution, but the current production adapter registry is intentionally empty. No real remediation action is currently available or claimed as operational.
7. **Gated Memory Retention:** The design restricts confirmed production-memory retention to eligible verified postmortems. A complete real-incident recovery and retention cycle has not yet been live-verified.

## Setup & Verification

### 1. Environment Configuration

Copy `.env.example` to `.env` and configure your credentials:
```powershell
# Required runtime configuration
POSTGRES_PASSWORD=your-secure-password
DATABASE_URL=postgresql://postgres:your-secure-password@127.0.0.1:5432/opsmemory
OPSMEMORY_API_KEYS=key-sre-secret:alice:sre,key-viewer-secret:bob:viewer,key-admin-secret:charlie:admin

# Provider credentials (optional for unit tests; required for live cloud calls)
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-120b
HINDSIGHT_BASE_URL=https://api.hindsight.vectorize.io
HINDSIGHT_API_KEY=...
HINDSIGHT_BANK_ID=opsmemory
HINDSIGHT_EXPERIMENT_BANK_ID=opsmemory-ab-experiment
```

### 2. Run Automated Test Suites

OpsMemory includes automated unit, provider-contract, and PostgreSQL integration tests (unit tests, provider contract tests, and a live PostgreSQL 16 integration test):

```powershell
powershell -File .\scripts\verify_source.ps1
```

### 3. Check Provider Connectivity Diagnostics

OpsMemory provides a safe diagnostic tool that checks all local and cloud providers without leaking secrets:

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_providers.py
```

### 4. Start Infrastructure via Docker Compose

```powershell
# Start database and backend containers
docker compose up -d

# Verify containers are healthy
docker compose ps
```

- Dashboard UI: `http://localhost:8000/`
- Health probe: `http://localhost:8000/health`
- Readiness probe: `http://localhost:8000/ready`
- Live provider diagnostics: `http://localhost:8000/diagnostics/providers` (requires `X-API-Key` header)

### 5. Hindsight A/B Memory Experiment — Executed

A live experiment compared two runs using the same synthetic scenario input and an isolated Hindsight experiment bank. Run A used no memory; Run B retrieved the exact seeded fixture. Evidence: `evidence/ab-experiment-status.json` and `evidence/before-after-raw.json`. This verifies the experiment and fixture recall, not production incident learning or a general performance improvement.

## Manual Submission Tasks

The following hackathon requirements require manual team actions:
- **Band AI Desktop:** Install and utilize Band AI desktop for project work and capture workflow screenshots.
- **GitHub Repository:** Push repository to public GitHub.
- **Video Walkthrough:** Record a 2–5 minute 1080p demo walkthrough showing the dashboard, incident investigation, Hindsight memory recall, approval workflow, and verification.
- **Articles & Social Posts:** Per-member article (800–1,500 words) and LinkedIn post (under 800 chars) with required links.

## Session 5 changes: recovery can no longer be "verified" by OpsMemory's own metrics

- `/incidents/{id}/verify` (admin) now **fails closed** unless ALL are true: (1) a *non-simulated* remediation was approved and executed for that incident; (2) every affected service has an entry in `RECOVERY_BINDINGS_JSON` (its own queries + `required_labels` proving the series identity); (3) the returned series carry those labels; (4) all samples post-date the remediation and are fresh. Queries using `opsmemory_*` metrics are rejected at load time. Without a bound payment-gateway exporter, a payment-gateway incident stays **unverified**.
- A post-mortem marks a root cause **confirmed** only when recovery was verified *and* a real remediation was executed. Otherwise the cause stays `unverified_hypothesis`. Only confirmed outcomes are retained as verified memories; a real remediation that failed verification is retained as an explicit *FAILED RESOLUTION ATTEMPT*. Retention errors are recorded on the incident (`memory_retention`), never reported as stored.
- Retained memories carry structured metadata (service, severity, environment, root-cause status, confidence, evidence event IDs, verification source, timestamp).
- Execution is claim-based (`approved -> executing -> executed`), so duplicate/concurrent execute calls cannot run an adapter twice (single backend process).
- `scripts/run_ab_experiment.py` refuses to run if the experiment bank equals the production bank, writes `evidence/ab-experiment-status.json` for every outcome, and exits non-zero (BLOCKED / FAILED / INVALID) instead of reporting results when the bank returns 403, the fixture can't be recalled, or the memory-on run doesn't retrieve the exact fixture.
- Security: rate limiting + failed-auth lockout, byte-safe key comparison, CSP/no-store headers, provider diagnostics restricted to operators, `/me` for role display. The dashboard's "inject demo" button now creates clearly labelled synthetic fixtures (`metadata.synthetic_fixture`) that are never retained to Hindsight.
- Note: `docker-compose.yml` still sets `RECOVERY_ERROR_RATE_QUERY`/`RECOVERY_P99_SECONDS_QUERY` to OpsMemory API metrics. `/verify` no longer uses them; remove them to avoid confusion.
