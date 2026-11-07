# Source-build environment report — 2026-11-06

Observed in the isolated source-build container:
- Python 3.13.5.
- FastAPI 0.128.2 and Pydantic 2.13.5 available for API tests.
- `psycopg` and `hindsight_client` are not installed in this isolated container.
- Docker CLI and PostgreSQL client/server binaries are not available in this isolated container.
- No live Groq or Hindsight credentials were supplied to this source-build environment.

Consequences:
- 45 automated tests and Python compilation were executed successfully.
- The PostgreSQL repository was source-reviewed but not run against PostgreSQL here.
- Hindsight/Groq live calls and Docker deployment remain unverified here.
- The user's separate Windows/WSL PostgreSQL 16 container was previously verified running in the user's environment; this does not prove the new repository has been deployed to it.
