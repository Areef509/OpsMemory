# OpsMemory — Live Verification Report
Date: 2026-11-07

## Verified with live execution

| Component | Result | Evidence |
|---|---|---|
| Docker backend | VERIFIED | Image built; container healthy |
| PostgreSQL | VERIFIED | Existing database and incident records readable |
| Prometheus | VERIFIED | Target http://backend:8000/metrics reported UP |
| API metrics | VERIFIED | Real request counter and latency histogram exported |
| Recovery metrics collector | VERIFIED | Three real samples collected: error_rate 0.0; p99 approximately 4.95 ms |
| Groq | VERIFIED | Real openai/gpt-oss-120b inference; 2.563 s model latency |
| Incident investigation | VERIFIED | Existing non-synthetic production incident 231cd9365056 investigated |
| Investigation persistence | VERIFIED | GET incident returned saved investigation from PostgreSQL |
| Hindsight connectivity | VERIFIED EARLIER | Authenticated provider diagnostic succeeded |
| Hindsight persistence | VERIFIED EARLIER | Synthetic diagnostic marker recalled after backend restart |

## Important limitations

- The Hindsight memory used by the live payment-gateway investigation contained only a synthetic persistence diagnostic marker. Relevant historical payment-gateway learning was not demonstrated.
- The production adapter registry is empty. Real remediation execution is NOT IMPLEMENTED / NOT VERIFIED.
- Recovery metrics currently describe the OpsMemory API, not payment-gateway. They must not be used to close this payment-gateway incident.
- No verified postmortem was produced or retained for the payment-gateway incident.
- No genuine memory-off versus memory-on A/B result has been verified.
- Band AI desktop usage has not been confirmed.
- Public GitHub publication has not been completed.
- The live incident investigation returned hypothesis_only. Its possible causes are hypotheses, not confirmed root causes.

## Live incident evidence

Incident: 231cd9365056
Service: payment-gateway
Environment: prod
Severity: SEV-2
Synthetic: false
Observed event: payment gateway timeout under load; reported error rate 4.5%.
Reported impact: approximately 1,500 users.
Investigation model: openai/gpt-oss-120b
Model latency: 2.563 seconds
Total investigation time: 6.402 seconds
Investigation state: hypothesis_only
Persisted state: investigated

## Submission integrity

Do not describe simulated or unimplemented adapters as operational.
Do not describe OpsMemory API metrics as payment-gateway recovery evidence.
Do not publish fabricated A/B percentages or historical incident results.
Do not claim a confirmed root cause or verified recovery for this incident.
