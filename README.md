# OpsMemory — Autonomous Incident Response & Continuous Memory

OpsMemory ingests operational telemetry, correlates related symptoms across microservice dependency topologies, assigns an explainable severity with confidence and unknowns, engages an anti-fabrication LLM (Groq) for causal investigation, continuously recalls prior incident knowledge and runbooks through Hindsight, and gates allow-listed remediation behind human-in-the-loop approval. Recovery is strictly verified from server-side Prometheus metrics before any verified post-mortem is retained to the production memory bank.

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
6. **Controlled Remediation Adapters:** Actions can execute only through explicitly registered, allow-listed adapters (`scale-pool`, `restart-service`, `clear-cache`, `demo-noop`). Unregistered adapters are rejected.
7. **Gated Memory Retention:** Synthetic test fixtures and unverified hypotheses are NEVER retained as confirmed production memories in Hindsight. Only post-mortems with verified metric recovery promote root causes to `confirmed` and retain to Hindsight.

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

OpsMemory includes 63 automated tests (unit tests, provider contract tests, and a live PostgreSQL 16 integration test):

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

### 5. Run the Hindsight A/B Memory Experiment

To demonstrate the quantitative and qualitative impact of Hindsight memory recall on incident investigation:

```powershell
.\.venv\Scripts\python.exe .\scripts\run_ab_experiment.py
```

The script:
- Seeds a verified historical resolution fixture in `HINDSIGHT_EXPERIMENT_BANK_ID`.
- Investigates a new similar incident without memory (`baseline_without_memory`).
- Investigates the incident with memory recall (`with_hindsight_memory`).
- Compares recommendations, hypotheses, and memory citations.
- Records actual provider telemetry and latency in `evidence/before-after-raw.json`.

## Manual Submission Tasks

The following hackathon requirements require manual team actions:
- **Band AI Desktop:** Install and utilize Band AI desktop for project work and capture workflow screenshots.
- **GitHub Repository:** Push repository to public GitHub.
- **Video Walkthrough:** Record a 2–5 minute 1080p demo walkthrough showing the dashboard, incident investigation, Hindsight memory recall, approval workflow, and verification.
- **Articles & Social Posts:** Per-member article (800–1,500 words) and LinkedIn post (under 800 chars) with required links.
