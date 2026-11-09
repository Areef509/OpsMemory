# Session 6 verification report
Environment: authoring sandbox (Python 3.12; no network, Docker, PostgreSQL, FastAPI, browser session, or access to E:\\opsmemory). Baseline taken from the supplied zip `opsmemory_aa6bc6.zip`.

## Baseline (before changes)
`python3 -m unittest discover -s tests/unit -t .` -> 102 ran, **1 failure + 1 error** (both from in-progress provenance work, not environment):
1. `MemoryPromptInjectionTests`: engine read `item.document_id/id/score` directly, so memory objects without those attributes aborted recall silently (recall error swallowed -> no memories). Fixed with `getattr` defaults.
2. `ABExperimentRegressionTests.test_exact_fixture_recalled_requires_exact_doc_id`: test still encoded the old text-match contract; `verify_exact_fixture_recalled` was deliberately tightened to structured provenance (exact `document_id` AND prompt index). Test updated to the stricter contract (free text containing the id now explicitly fails).

## Finding
`Engine.investigate` appends a hard-coded canary recommendation to LLM output. It is now labelled `origin: static_runbook:canary-startup-fault` with a note that it is neither LLM-generated nor memory-derived.

## After changes
Full unit suite: see `sandbox-unit-tests-session6.txt` (**142 ran, 0 failed, 16 skipped**; skips need FastAPI or a live backend).
Mutation checks on the audit verifier (skip hash recompute / link check / sequence check): each made tests fail, then restored.
Defect found by the new tests: an expired approval stayed "pending" in the incident record and blocked any fresh request; fixed.

## Scorecard
| Section | Status | Notes |
|---|---|---|
| A Browser workflow (Playwright) | NOT IMPLEMENTED | No browser tests written or run; needs the live stack |
| B Hindsight learning | PARTIAL | No new live runs; provenance fields fixed for robustness |
| C A/B clean-bank replication | PARTIAL / BLOCKED | Needs a dedicated experiment-only Hindsight key and fresh bank (human action); outdated unit test fixed |
| D Root-cause evidence | PARTIAL | Existing causal-evidence gate retained; postmortem v2 exposes hypotheses, rejected hypotheses, attestations separately; no new evidence collectors |
| E Adapter framework | PARTIAL | Registry + enforcement unit-verified; opt-in; only the canary adapter exists; no second real target |
| F Recovery verification | PARTIAL | Unchanged this session; payment-gateway still BLOCKED (no exporter) |
| G Postmortem | PARTIAL | v2 + history unit-verified; not live; no lessons invented |
| H Audit chain | PARTIAL | Logic and tamper detection unit-verified; migration/triggers/roles NOT applied or tested on PostgreSQL |
| I Auth / credential rotation | PARTIAL / BLOCKED | Earlier tests stand; Groq/DB credential rotation not performed (needs provider and DB coordination) |
| J Rate limiting | PARTIAL | Limiter unit-tested; per-process, resets on restart; keyed on socket IP (X-Forwarded-For ignored); no load test |
| K Dependency scan | NOT DONE | No scanner/network here |
| L Container scan | NOT DONE | No Docker here |
| M Isolated integration env | NOT IMPLEMENTED | |
| N Tenant isolation | NOT IMPLEMENTED | |
| O Payment-gateway recovery | BLOCKED | Needs real exporter, authorised adapter target, approval |
| P Band AI / GitHub / demo / content | PENDING | Human actions; nothing published |

Nothing in this report is safe-for-production evidence: no change has been applied to a live service.
