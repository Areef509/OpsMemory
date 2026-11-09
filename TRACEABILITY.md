# OpsMemory — Engineering Traceability Matrix

Status vocabulary:
- VERIFIED: supported by actual execution evidence.
- PARTIALLY VERIFIED: some behavior works; end-to-end requirement remains incomplete.
- IMPLEMENTED — NOT LIVE-VERIFIED: code exists, but real execution is not proven.
- NOT IMPLEMENTED: required operational capability is absent.
- NOT CONFIRMED: a required manual submission action has not been evidenced.

| Requirement | Current status | Evidence / limitation |
|---|---|---|
| FastAPI backend | VERIFIED | Running Docker backend and API health checks |
| Docker deployment | VERIFIED | Backend and PostgreSQL containers healthy |
| Event ingestion and persistence | VERIFIED | Existing PostgreSQL incident/event records |
| Event deduplication and redaction | VERIFIED IN TESTS | Unit tests; live coverage is narrower |
| Severity and correlation | PARTIALLY VERIFIED | Unit tests and existing incident data |
| Groq live inference | VERIFIED | Real openai/gpt-oss-120b API inference |
| Groq incident investigation | VERIFIED | Incident 231cd9365056; hypothesis_only result |
| Investigation persistence | VERIFIED | Investigation returned from PostgreSQL |
| Hindsight API connectivity | VERIFIED | Authenticated provider diagnostics |
| Hindsight retain and recall | VERIFIED | Diagnostic marker retained and recalled |
| Hindsight persistence after backend restart | VERIFIED | Same diagnostic marker recalled after restart |
| Relevant historical incident learning | NOT VERIFIED | Real investigation did not retrieve relevant historical incident knowledge |
| PostgreSQL persistence | VERIFIED | Existing PostgreSQL 16 database |
| Prometheus deployment and scraping | VERIFIED | Prometheus target backend:8000/metrics UP |
| Prometheus API metrics collection | VERIFIED | Three samples collected; API error rate 0.0, p99 approximately 4.95 ms |
| Payment-gateway recovery verification | PARTIALLY VERIFIED | Independent test gateway telemetry works; production recovery remains unverified |
| Production remediation adapters | NOT IMPLEMENTED | Production adapter registry is empty |
| Human approval and RBAC | PARTIALLY VERIFIED | Unit tests cover roles; complete live execution unavailable without adapters |
| Full postmortem workflow | VERIFIED IN DISPOSABLE POSTGRESQL | `backend/tests/integration/test_full_workflow_postgres.py`: 1 passed; production recovery/retention remains unverified |
| Memory-on versus memory-off A/B | VERIFIED — SYNTHETIC EXPERIMENT | Same-input live Groq/Hindsight runs; exact seeded fixture recalled; evidence in `evidence/ab-experiment-status.json` |
| Dashboard | PARTIALLY VERIFIED | Dashboard route and basic tests; complete live workflow not verified |
| Security | PARTIALLY VERIFIED | Security tests and review exist; comprehensive independent audit not completed |
| Unit tests | VERIFIED | 68 unit tests passed in the recorded run |
| Public GitHub repository | NOT PUBLISHED | No Git remote configured |
| Demo video | NOT CONFIRMED | Recording and submission not evidenced |
| Band AI desktop | NOT CONFIRMED | Participant usage not evidenced |

## Live incident evidence

- Incident: 231cd9365056
- Service: payment-gateway
- Environment: prod
- Severity: SEV-2
- Synthetic: false
- Event: payment gateway timeout under load; reported error rate 4.5%
- Reported impact: approximately 1,500 users
- Model: openai/gpt-oss-120b
- Model latency: 2.563 seconds
- Total investigation: 6.402 seconds
- Result: hypothesis_only
- Investigation persisted in PostgreSQL.

## Important trust boundaries

The Prometheus samples collected so far describe the OpsMemory API. They do not establish payment-gateway recovery and must not close the payment-gateway incident.

The Hindsight restart test used a synthetic diagnostic marker. It proves persistence and recall of that marker, not historical incident learning.

No production remediation action, confirmed root cause, verified recovery, or real postmortem is claimed.

See evidence/live-verification-2026-11-07.md for the detailed execution report.

## Session 5 additions (code-level; nothing below was executed against live services)

| Requirement | Current status | Evidence / limitation |
|---|---|---|
| Recovery verification bound per service, identity-checked, post-remediation, fresh | IMPLEMENTED — NOT LIVE-VERIFIED | `core/recovery.py`; 7 stub-Prometheus tests (`test_session5_gates.Bindings`) |
| Self-metrics (opsmemory_*) rejected as recovery evidence | VERIFIED IN TESTS | `load_bindings` tests |
| Verify blocked without real executed remediation | VERIFIED IN TESTS | `VerificationGate` tests + mutation check (removing the gate fails tests) |
| Root cause "confirmed" only with verified recovery + real remediation | VERIFIED IN TESTS | `test_recovery_without_remediation_does_not_confirm_root_cause` |
| Failed remediation attempts retained as failures; retention errors visible | VERIFIED IN TESTS (test doubles) | Not exercised against live Hindsight |
| Structured Hindsight metadata (service, env, severity, confidence, evidence IDs) | VERIFIED IN TESTS (stub client) | Not exercised against live Hindsight |
| Duplicate/concurrent execution safe (single process) | VERIFIED IN TESTS | thread test + mutation check; multi-replica needs DB compare-and-set |
| A/B experiment fails closed; refuses production bank | VERIFIED IN TESTS (refusal) / smoke-run locally | Real run still BLOCKED by Hindsight 403 on `opsmemory-ab-experiment` |
| Rate limiting, key-compare fix, CSP, diagnostics restricted | VERIFIED IN TESTS (limiter); API tests written, skipped without FastAPI | `test_api_session5.py` must be run in the project `.venv` |
| Dashboard honesty (hypothesis label, retention status, synthetic demo) | IMPLEMENTED — NOT LIVE-VERIFIED | Not browser-tested |
| Payment-gateway independent monitoring source | BLOCKED | No payment-gateway exporter/metrics endpoint exists; bindings are unset |
| Production remediation adapter | BLOCKED | No authorised, bounded, reversible target identified; registry intentionally empty |

## Session 6 additions (sandbox-verified code; nothing applied to your live database or containers)

| Requirement | Current status | Evidence / limitation |
|---|---|---|
| Hash-chained audit records (SHA-256, canonical JSON, seq, prev-hash) | VERIFIED IN UNIT TESTS | `test_audit_chain.py` (17 tests) + mutation checks |
| Tamper detection: modified, deleted, reordered, forged, bad hash, broken link | VERIFIED IN UNIT TESTS (disposable in-memory chain / temp JSON) | not run against PostgreSQL |
| Tail-truncation / full-rewrite detection | PARTIAL BY DESIGN | needs an external anchor (`--expect-head`, `--expect-count`); documented |
| PostgreSQL append path and immutability trigger (003 migration) | VERIFIED IN DISPOSABLE POSTGRESQL | Migration applied and append-only behavior observed; restricted-role grants remain unverified |
| Declarative adapter registry (target, action, role, risk, timeout, scope, rollback, verification) | VERIFIED IN UNIT TESTS | `test_adapter_registry.py` (18 tests); wiring is opt-in |
| Cross-service / wrong-environment / synthetic / duplicate / expired / insufficient-role refusals | VERIFIED IN UNIT TESTS | engine-level; no real second adapter exists |
| Structured postmortem v2 + preserved history | VERIFIED IN UNIT TESTS | `test_postmortem_v2.py`; lessons/preventive actions stay empty unless an operator supplies them |
| Tenant isolation | VERIFIED IN DISPOSABLE POSTGRESQL | Cross-tenant incident, event, approval, experiment, and audit visibility tests passed |
| Browser end-to-end workflow tests | NOT IMPLEMENTED | |
| Isolated integration test environment | NOT IMPLEMENTED | |
| Dependency / container scans, credential rotation | NOT DONE (needs network/provider access) | |
