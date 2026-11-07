# Security review — implementation review, not a penetration test

Implemented and covered by automated tests:
- API-key authentication with constant-time comparison and explicit `viewer`/`sre`/`admin` roles.
- Viewers cannot ingest or operate incidents; only admins can trigger recovery verification.
- Recovery metrics are collected server-side from a configured Prometheus-compatible endpoint. Caller-supplied recovery samples and synthetic flags are not accepted by the verification route.
- Synthetic ingestion mode is server-configured, not caller-controlled; synthetic incidents are not promoted to verified production Hindsight memory.
- Recursive redaction of nested fields named password/passwd/secret/api_key/token/authorization, plus message-pattern redaction.
- Event text is treated as untrusted data and checked for prompt-injection patterns; model hypotheses must cite real event IDs.
- AI-generated action text is not executed directly. Only registered adapter names can be requested, and approval is required.
- Runtime uses PostgreSQL explicitly; there is no silent in-memory fallback.
- Dashboard API key remains in page memory and is not persisted in local storage.

Still required before production:
- Live PostgreSQL migration and SQL review; dependency and container vulnerability scans; rate limiting; secret manager/rotation; TLS and reverse-proxy configuration; audit-log tamper-evidence; tenant isolation; operational access review; service-specific remediation connector security review.
- The optional `demo-noop` adapter is simulated and must not be mistaken for a real remediation.

No claim of zero vulnerabilities or production certification is made.
