# OpsMemory — Hackathon Submission Deliverables

This document is the honest submission checklist and recording guide. It distinguishes demonstrated functionality from unfinished capabilities.

## 1. Technical article draft

**Title:** OpsMemory: Building an AI-Assisted Incident Investigation Agent with Persistent Memory

### The problem

Incident responders repeatedly investigate service failures. An AI assistant that starts every investigation without relevant operational history can repeat generic reasoning and fail to reuse previously established knowledge.

OpsMemory explores an architecture combining Groq inference, Hindsight persistent memory, PostgreSQL incident records, and Prometheus monitoring.

### Architecture

- FastAPI provides the application API.
- PostgreSQL stores incidents, events, and investigation results.
- Groq provides live LLM inference using `openai/gpt-oss-120b`.
- Hindsight provides persistent semantic retain and recall.
- Prometheus scrapes application metrics.
- A browser dashboard provides an incident workspace.

### What was demonstrated

A genuine, non-synthetic production incident (`231cd9365056`) for the payment-gateway service was investigated through the live Groq integration. The investigation returned three possible causes, five unknowns, and four recommendations. Its result was persisted in PostgreSQL.

The investigation result was `hypothesis_only`; none of the candidate causes was confirmed.

Hindsight retain and recall were exercised using a clearly labeled synthetic diagnostic marker. The marker remained retrievable after a backend restart. This demonstrates persistence, but does not demonstrate successful recall of a relevant historical payment-gateway postmortem.

Prometheus successfully scraped the OpsMemory API, and its recovery collector obtained three samples. Those measurements describe the OpsMemory API, not the payment-gateway service.

### Engineering safeguards

The project includes role-based access controls, input validation, redaction logic, security headers, and tests. The recorded unit-test run passed 68 tests. Passing unit tests do not establish that every external integration or end-to-end workflow is complete.

### Limitations

The production remediation adapter registry is intentionally empty. Production remediation and payment-gateway recovery remain unverified. Relevant historical incident learning is not proven. The full workflow integration test passed against disposable PostgreSQL, and the isolated synthetic-fixture memory-on/memory-off A/B experiment was executed; neither result proves production recovery or production postmortem retention.

These limitations are important: OpsMemory should currently be described as an AI-assisted incident investigation system with persistent-memory integration, not as a fully autonomous closed-loop remediation platform.

## 2. Social post draft

OpsMemory explores AI-assisted incident investigation with Groq, Hindsight persistent memory, PostgreSQL, and Prometheus.

We verified live LLM investigation of a genuine production incident and persistence of its investigation in PostgreSQL. We also tested Hindsight retain/recall across a backend restart.

The project is still evolving: relevant historical incident learning, real remediation, and target-service recovery verification remain open engineering tasks.

#SRE #AIOps #IncidentResponse #Hindsight #DevOps

Replace this draft with the actual public repository URL after publication. Do not claim unmeasured A/B improvements.

## 3. Honest 2–5 minute demo recording script

### 0:00–0:30 — Introduction
Introduce OpsMemory as an AI-assisted incident investigation project using Groq, Hindsight, PostgreSQL, and Prometheus.

### 0:30–1:30 — Real incident investigation
Show the existing incident `231cd9365056`. Explain that it is a non-synthetic payment-gateway incident. Show the real Groq investigation result, hypotheses, unknowns, recommendations, and the `hypothesis_only` status.

### 1:30–2:10 — Persistence
Show the saved investigation in PostgreSQL. Explain that the investigation result survived as a persisted incident record.

### 2:10–2:50 — Hindsight
Explain the Hindsight integration and show the actual diagnostic retain/recall persistence evidence. Clearly disclose that this marker was synthetic and that relevant historical incident learning is not yet proven.

### 2:50–3:30 — Monitoring
Show Prometheus target health and collected API metrics. Explicitly explain that these metrics describe OpsMemory itself, not the payment-gateway service.

### 3:30–4:15 — Engineering limitations
Show the traceability matrix. Explain that production remediation adapters are empty and target-service recovery verification and postmortem retention are unfinished.

### 4:15–4:45 — Conclusion
Summarize the verified work and the next engineering milestones. Do not demonstrate fake adapter execution, invented historical incidents, or fabricated recovery.

## 4. Team deliverable checklist

| Deliverable | Status |
|---|---|
| Source repository | Local project exists; public publication pending |
| Technical article | Draft above; requires team review and publication |
| Social post | Draft above; publication pending |
| Demo video | Must be genuinely recorded and submitted |
| Band AI desktop | Each participant must complete and confirm required usage |
| Public repository URL | Pending creation and publication |
| Reddit/share URL | Pending, if required by the official guide |

Do not mark a manual deliverable complete until the participant has actually completed it.

## Status update (session 5): unchanged human actions still outstanding
Band AI desktop usage: NOT CONFIRMED. Public GitHub repo: NOT PUBLISHED (needs your explicit confirmation and repo name). Demo video, article, LinkedIn post, Reddit link post: NOT DONE. Hindsight experiment bank access: BLOCKED (HTTP 403, admin action). Do not describe any of these as complete.
