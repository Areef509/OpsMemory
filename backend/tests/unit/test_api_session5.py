"""API-level checks for session-5 hardening. Skipped when FastAPI is not installed (it is in the project .venv and
Docker image). These use TestClient in-process and only touch endpoints that do not need PostgreSQL."""
import os, unittest
from unittest.mock import patch
try:
    from fastapi.testclient import TestClient
    from app import main
    HAVE = True
except ModuleNotFoundError:
    HAVE = False


@unittest.skipUnless(HAVE, "fastapi not installed")
class Session5Api(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPSMEMORY_API_KEYS": "viewer-k:bob:viewer,sre-k:ann:sre,admin-k:root:admin"}); self.env.start()
        main._rl.__init__(10_000); main._auth_fail.__init__(10); self.c = TestClient(main.app)

    def tearDown(self): self.env.stop()

    def test_non_ascii_key_is_401_not_500(self):
        from starlette.requests import Request
        from fastapi import HTTPException

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/me",
            "raw_path": b"/me",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        }
        request = Request(scope)
        with self.assertRaises(HTTPException) as caught:
            main.auth(request, "k\u00e9y")
        self.assertEqual(caught.exception.status_code, 401)
    def test_me_reports_role(self): self.assertEqual(self.c.get("/me", headers={"X-API-Key": "viewer-k"}).json(), {"name": "bob", "role": "viewer"})

    def test_auth_failure_lockout(self):
        for _ in range(10): self.c.get("/me", headers={"X-API-Key": "bad"})
        self.assertEqual(self.c.get("/health").status_code, 429)

    def test_general_rate_limit(self):
        main._rl.__init__(3); codes = [self.c.get("/health").status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_viewer_cannot_call_live_provider_diagnostics(self):
        self.assertEqual(self.c.get("/diagnostics/providers", headers={"X-API-Key": "viewer-k"}).status_code, 403)

    def test_verify_requires_admin_not_sre(self):
        self.assertEqual(self.c.post("/incidents/x/verify", headers={"X-API-Key": "sre-k"}).status_code, 403)
        self.assertEqual(self.c.post("/incidents/x/verify", headers={"X-API-Key": "viewer-k"}).status_code, 403)

    def test_viewer_cannot_investigate_incidents(self):
        r = self.c.post("/incidents/nonexistent-id/investigate",
                        headers={"X-API-Key": "viewer-k"})
        self.assertEqual(r.status_code, 403)

    def test_viewer_cannot_request_remediation(self):
        r = self.c.post("/incidents/nonexistent-id/remediations",
                        json={"recommendation_index": 0, "adapter": "canary-clear-fault"},
                        headers={"X-API-Key": "viewer-k"})
        self.assertEqual(r.status_code, 403)

    def test_viewer_cannot_decide_or_execute_approvals(self):
        headers = {"X-API-Key": "viewer-k"}
        decision = self.c.post("/approvals/nonexistent-id/decision",
                               json={"approve": True, "reason": "RBAC test"},
                               headers=headers)
        execution = self.c.post("/approvals/nonexistent-id/execute",
                                headers=headers)
        self.assertEqual(decision.status_code, 403)
        self.assertEqual(execution.status_code, 403)

    def test_headers(self):
        r = self.c.get("/health"); self.assertIn("default-src 'self'", r.headers["content-security-policy"]); self.assertEqual(r.headers["cache-control"], "no-store")

    def test_wildcard_cors_not_enabled(self):
        r = self.c.get("/health", headers={"Origin": "https://evil.example"}); self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})


if __name__ == "__main__":
    unittest.main()
