# Test Report — Verified Implementation

## Execution Evidence

Executed on: `2026-11-06`
Command:
```powershell
powershell -File .\scripts\verify_source.ps1
```

Result: **63 tests passed, 0 failed** (62 unit tests, 1 live PostgreSQL 16 integration test).

### Verified Test Suites:
1. `backend/tests/unit/test_core.py` (10 tests)
   - Idempotency, normalization, and recursive redaction.
   - Correlation across microservice dependency topologies.
   - Severity scoring, score explanation, and unknowns enumeration.
2. `backend/tests/unit/test_engine.py` (6 tests)
   - Lifecycle orchestration and memory flow across investigations.
   - Fail-closed memory outage reporting (never hidden).
   - Unregistered adapter refusal.
   - Gated retention: unverified recovery never retained.
   - Untrusted prompt-injection sanitization.
3. `backend/tests/unit/test_pipeline.py` (22 tests)
   - Anti-fabrication validator: drops fabricated event IDs and unsupported hypotheses.
   - Invalid memory reference removal.
   - Groq client retry with backoff, authentication, 429/5xx handling.
   - Human approval RBAC: viewer role denied and logged.
   - Remediation adapter execution and simulated tagging.
   - Consecutive recovery metric verification and post-mortem generation.
4. `backend/tests/unit/test_providers_integration.py` (13 tests)
   - Secret masking for diagnostics (never leaks API keys).
   - JSON markdown fence (````json````) extraction and reasoning tag (`<think>`) stripping.
   - Groq connectivity probe (`check_connection`).
   - Hindsight connectivity probe and version detection.
   - Hindsight automatic bank initialization with SRE mission profile.
   - Hindsight rich recall result parsing (scores, document IDs, metadata).
   - A/B memory ablation: demonstrates how recalled historical memory shapes recommendations.
5. `backend/tests/unit/test_runtime_hardening.py` (11 tests)
   - Durable store restart protocol.
   - Prompt injection marking on retrieved memories.
   - Prometheus server-side metrics verification and unit conversion.
   - FastAPI role enforcement (viewer denial on `/events`).
   - Server-side synthetic flag control.
6. `backend/tests/integration/test_full_workflow_postgres.py` (1 test)
   - **Live PostgreSQL 16 container execution (`opsmemory-db-1`)**.
   - Transactional event persistence into `events` table with deduplication.
   - Verified recursive redaction in PostgreSQL JSONB column.
   - Multi-service candidate correlation.
   - Groq causal investigation with candidate event ID grounding.
   - SRE human approval workflow and immutable `audit_log` verification.
   - Remediation execution and simulated result persistence.
   - Recovery verification and post-mortem generation with confirmed root cause.
   - Gated retention to Hindsight memory bank.
   - Engine restart persistence: reloads candidate from PostgreSQL database.
