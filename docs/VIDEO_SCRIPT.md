# OpsMemory demo recording script (2–5 minutes)

1. Introduce OpsMemory and explain the incident investigation workflow.
2. Show the application and its incident/event views using a safe demo or synthetic dataset.
3. Show the PostgreSQL integration-test result for the full postmortem workflow.
4. Show tenant-isolation test evidence from the disposable PostgreSQL environment.
5. Show the audit-chain verification evidence and explain that it is from a disposable database.
6. Show `evidence/ab-experiment-status.json` and the A/B raw results. Explain that this is a synthetic fixture experiment, not proof of production learning or improved quality.
7. Close with limitations: production Prometheus is not configured; production root cause and production remediation are not verified.

Do not display credentials, API keys, personal data, or production secrets. Do not claim production recovery or remediation.
