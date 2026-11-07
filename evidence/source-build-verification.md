# Source-Build & Infrastructure Verification Evidence — 2026-11-06

Executed in the host and containerized environment:

| Check | Result | Evidence / Details |
|---|---|---|
| `powershell -File .\scripts\verify_source.ps1` | **63 passed, 0 failed** | 62 unit/contract tests + 1 full PostgreSQL integration test |
| Python bytecode compilation (`compileall`) | **Passed** | `backend/app` and `scripts` cleanly compiled |
| PostgreSQL 16 Container (`opsmemory-db-1`) | **Healthy (Up)** | Tables `events`, `incidents`, `approvals`, `audit_log` active |
| Schema Migration `002_runtime_integrity.sql` | **Applied & Verified** | Indexes `incidents_created_at`, `events_ts`, `audit_log_ts` verified in DB |
| Backend Docker Image Build (`opsmemory-backend`) | **Built** | Multi-stage build completed with FastAPI, psycopg, hindsight-client |
| Backend Container (`opsmemory-backend-1`) | **Healthy (Up)** | Running on `127.0.0.1:8000` |
| Health Probe (`GET /health`) | **HTTP 200** | `{"status": "alive"}` |
| Readiness Probe (`GET /ready`) | **HTTP 200** | `{"status": "database_ready", "postgres": "connected"}` |
| Safe Provider Diagnostics (`GET /diagnostics/providers`) | **HTTP 200** | Non-leaking probe reporting status of DB, Groq, Hindsight, and Prometheus |
| Registered Remediation Adapters (`GET /adapters`) | **HTTP 200** | `scale-pool`, `restart-service`, `clear-cache`, `demo-noop` |
| SRE Cockpit Dashboard (`GET /`) | **HTTP 200** | Full dark-mode SRE UI with live telemetry, approval controls, and memory highlight |
| Live API Event Ingestion (`POST /events`) | **HTTP 200** | Event normalized, deduplicated, and persisted to PostgreSQL |
| Live Incident Fetch (`GET /incidents/{id}`) | **HTTP 200** | Correlated incident returned with timeline and approval requests |
| Full PostgreSQL Integration Test | **Passed** | Ingestion -> correlation -> investigation -> approval -> remediation -> verification -> post-mortem -> retention |
| Groq Client Contract & Diagnostics | **Passed** | 429 Retry-After, markdown extraction, reasoning tag stripping verified |
| Hindsight Client Contract & Diagnostics | **Passed** | 1.2.0 SDK integration, bank auto-creation, rich recall parsing verified |
| Git Repository Initialization | **Initialized** | Clean repository; `.env` and `.venv` strictly ignored via `.gitignore` |
