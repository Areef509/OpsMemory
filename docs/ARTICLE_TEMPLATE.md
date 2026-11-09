# OpsMemory: Evidence-Grounded Incident Operations

OpsMemory is an incident investigation and operational memory project designed to help teams organize incident evidence, investigate hypotheses, preserve audit history, and produce postmortem records.

## Verified implementation

- PostgreSQL-backed incident workflow and integration testing in a disposable database.
- Tenant-isolation checks across incident, event, approval, experiment, and audit visibility.
- Append-only audit behavior tested in disposable PostgreSQL.
- Full incident-to-postmortem workflow integration test passed.
- A live synthetic-fixture A/B experiment used the same scenario input with memory disabled and enabled. The memory-enabled run retrieved the seeded fixture. This demonstrates fixture retrieval, not a general improvement in investigation quality.
- Groq and Hindsight provider connectivity and fixture retrieval were verified.

## Limitations

Production payment-gateway recovery and production remediation have not been verified. Production root cause has not been established from historical evidence. The current provider verification reports Prometheus as not configured. Disposable-database tests must not be represented as production verification.

## Conclusion

OpsMemory demonstrates a tested incident workflow and isolated memory experiment. Production monitoring, authorized remediation, and production incident validation remain separate work.
