# Current Verification Boundaries & Manual Deliverables

## Verified in Current Implementation & Infrastructure

1. **Full Incident Workflow on Live PostgreSQL 16**:
   - Ingestion, deduplication, and recursive redaction in PostgreSQL JSONB.
   - Microservice event correlation and deterministic severity evaluation.
   - LLM anti-fabrication hypothesis validation and memory reference checking.
   - SRE human-in-the-loop approval workflow and immutable `audit_log` persistence.
   - Controlled remediation adapter execution with simulation tagging.
   - Consecutive recovery metric verification and post-mortem generation.
   - Restart durability: engine candidate recovery from PostgreSQL.
2. **Automated Test Coverage**:
   - **63 tests passing** (62 unit/contract tests and 1 full PostgreSQL integration test).
3. **Provider Layer Enhancements**:
   - Groq client: 429 rate limit `Retry-After` handling, markdown fence extraction, reasoning tag stripping, safe diagnostics probe, secret masking, and fail-closed error handling.
   - Hindsight memory client: 1.2.0 SDK integration, automatic bank profile initialization (`retain_mission` / `reflect_mission`), semantic recall with score extraction, and gated retention.
4. **Executive SRE Dashboard**:
   - Real-time dark-mode cockpit (`frontend/index.html`) deployed and responding HTTP 200 on `http://127.0.0.1:8000/`.
   - Distinct, prominent Hindsight Continuous Memory highlight container.
   - Interactive investigation, approval/rejection controls, and live demo injection.
5. **Infrastructure**:
   - `opsmemory-db-1` (PostgreSQL 16) and `opsmemory-backend-1` (FastAPI) both healthy and running under Docker Compose.
   - Migration `002_runtime_integrity.sql` verified with DB indexes.

## Awaiting Local Credentials in `.env`

- **Live External Groq API**: Real LLM calls to `api.groq.com` activate when `GROQ_API_KEY` is populated in `.env`.
- **Live External Hindsight Service**: Real semantic recall and post-mortem retention against external Hindsight instance activate when `HINDSIGHT_BASE_URL` is set in `.env`.
- **Live Prometheus Instance**: Real Prometheus range queries activate when `PROMETHEUS_BASE_URL` and PromQL queries are set in `.env`.

> [!NOTE]
> The codebase intentionally does **not** silently mock these providers when credentials are missing. Live calls fail closed with clear, actionable diagnostics, and diagnostic endpoints/scripts report the exact configuration state without leaking secrets.

## Remaining Manual Hackathon Deliverables

The following requirements from the HackwithHyderabad 3.0 Hackathon Content Guide require manual team actions:
1. **Band AI Desktop Workflow (R2)**: Team must open Band AI desktop, log activities, and capture screenshots for submission evidence.
2. **Public GitHub Repository (R7)**: Team must add a public remote (`git remote add origin ...`) and push to GitHub.
3. **Public YouTube Demo Video (R8)**: Team must record a 2–5 minute 1080p demo walkthrough showing the dashboard, incident investigation, Hindsight memory recall, approval workflow, and verification.
4. **Judge Demo Walkthrough (R9)**: Deploy on presentation machine and rehearse live walkthrough.
5. **Per-Member Public Article (R11)**: Each team member must write and publish an 800–1,500 word technical article with required links/tags.
6. **Per-Member LinkedIn Post (R12)**: Each member must publish a LinkedIn post under 800 characters with required repository link.
7. **Subreddit Share (R13)**: Share article on one authorized subreddit.
