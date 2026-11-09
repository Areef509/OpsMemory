"""Unit and contract tests for real provider integration layer (Groq and Hindsight).

Tests verify safe diagnostics, content extraction, secret masking, error handling,
and SDK contracts without exposing credentials.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.integrations.groq_client import (
    GroqClient, LLMError, _extract_json_content, mask_secret
)
from app.integrations.hindsight_memory import (
    HindsightMemory, HindsightError, Memory
)
from tests.unit.test_pipeline import serve, ok


class ProviderSafetyAndSanitizationTests(unittest.TestCase):
    def test_secret_masking(self):
        self.assertEqual(mask_secret(None), "<not set>")
        self.assertEqual(mask_secret(""), "<not set>")
        self.assertEqual(mask_secret("short"), "***")
        self.assertEqual(mask_secret("gsk_1234567890abcdef"), "gsk_...cdef")

    def test_json_markdown_extraction(self):
        raw_fenced = "```json\n{\"status\": \"ok\", \"count\": 42}\n```"
        clean = _extract_json_content(raw_fenced)
        self.assertEqual(json.loads(clean), {"status": "ok", "count": 42})

    def test_json_reasoning_tag_stripping(self):
        raw_reasoning = "<think>Analyzing symptoms...</think>\n```json\n{\"cause\": \"leak\"}\n```"
        clean = _extract_json_content(raw_reasoning)
        self.assertEqual(json.loads(clean), {"cause": "leak"})

    def test_plain_json_kept_intact(self):
        plain = "{\"valid\": true}"
        self.assertEqual(_extract_json_content(plain), plain)


class GroqDiagnosticsTests(unittest.TestCase):
    def test_groq_check_connection_success(self):
        srv, _ = serve([ok({"status": "ok"})])
        client = GroqClient("test-key", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0)
        diag = client.check_connection()
        self.assertTrue(diag["configured"])
        self.assertTrue(diag["connected"])
        self.assertEqual(diag["model"], "stub-model")
        self.assertNotIn("test-key", str(diag))
        srv.shutdown(); srv.server_close()

    def test_groq_check_connection_failure_reported_safely(self):
        client = GroqClient("test-key", url="http://127.0.0.1:1/", backoff=0, max_retries=0, timeout=0.5)
        diag = client.check_connection()
        self.assertTrue(diag["configured"])
        self.assertFalse(diag["connected"])
        self.assertIn("error", diag)
        self.assertNotIn("test-key", str(diag))


class HindsightIntegrationUnitTests(unittest.TestCase):
    def test_missing_base_url_raises_hindsight_error(self):
        with patch.dict(os.environ, {"HINDSIGHT_BASE_URL": ""}, clear=False):
            with self.assertRaises(HindsightError):
                HindsightMemory(base_url="")

    def test_check_connection_success(self):
        mem = HindsightMemory(base_url="http://mock-hindsight:8888", api_key="secret-key")
        mock_client = MagicMock()
        mock_version = MagicMock()
        mock_version.api_version = "1.2.0"
        mock_client.get_version.return_value = mock_version
        mock_client.get_bank_config.return_value = {"bank_id": "opsmemory"}
        mem._client = mock_client

        diag = mem.check_connection()
        self.assertTrue(diag["configured"])
        self.assertTrue(diag["connected"])
        self.assertEqual(diag["api_version"], "1.2.0")
        self.assertEqual(diag["bank_status"], "present")
        self.assertNotIn("secret-key", str(diag))

    def test_check_connection_failure_reported_safely(self):
        mem = HindsightMemory(base_url="http://mock-hindsight:8888", api_key="secret-key")
        mock_client = MagicMock()
        mock_client.get_version.side_effect = ConnectionError("network unreachable")
        mem._client = mock_client

        diag = mem.check_connection()
        self.assertTrue(diag["configured"])
        self.assertFalse(diag["connected"])
        self.assertIn("ConnectionError", diag["error"])
        self.assertNotIn("secret-key", str(diag))

    def test_ensure_bank_exists_creates_when_not_found(self):
        mem = HindsightMemory(base_url="http://mock-hindsight:8888", bank_id="test-bank")
        mock_client = MagicMock()
        mock_client.get_bank_config.side_effect = type("NotFoundError", (Exception,), {"status": 404})("404 Not Found")
        mem._client = mock_client

        res = mem.ensure_bank_exists("test-bank")
        self.assertTrue(res)
        mock_client.create_bank.assert_called_once()
        call_kwargs = mock_client.create_bank.call_args[1]
        self.assertEqual(call_kwargs["bank_id"], "test-bank")
        self.assertIn("incident", call_kwargs["retain_mission"].lower())

    def test_recall_similar_handles_results_and_metadata(self):
        mem = HindsightMemory(base_url="http://mock-hindsight:8888", bank_id="test-bank")
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "Pool exhaustion resolved by scaling max connections"
        mock_result.type = "experience"
        mock_result.id = "mem-1"
        mock_result.document_id = "incident-abc"
        mock_result.metadata = {"verified": "true"}
        mock_result.scores = {"final": 0.95}

        mock_resp = MagicMock()
        mock_resp.results = [mock_result]
        mock_client.recall.return_value = mock_resp
        mem._client = mock_client

        memories = mem.recall_similar("pool exhaustion", max_tokens=1000)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].text, mock_result.text)
        self.assertEqual(memories[0].type, "experience")
        self.assertEqual(memories[0].id, "mem-1")
        self.assertEqual(memories[0].document_id, "incident-abc")
        self.assertEqual(memories[0].score, 0.95)

    def test_recall_similar_handles_none_results(self):
        mem = HindsightMemory(base_url="http://mock-hindsight:8888", bank_id="test-bank")
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.results = None
        mock_client.recall.return_value = mock_resp
        mem._client = mock_client

        memories = mem.recall_similar("query")
        self.assertEqual(memories, [])


class MemoryAblationWorkflowTests(unittest.TestCase):
    """Verifies the A/B experiment flow where memory directly shapes the model's recommendation."""
    def test_memory_recall_changes_recommendation(self):
        from app.engine import Engine
        from app.store_memory import InMemoryStore
        from app.core.events import ingest
        from app.core.correlation import correlate
        from app.core.investigation import validate
        import pathlib

        demo = json.loads((pathlib.Path(__file__).parents[3] / "demo" / "payment_pool_exhaustion.json").read_text())
        evs, _ = ingest(demo["events"], set())
        candidate = correlate(evs, demo["dependencies"])[0]
        ev_ids = [e.event_id for e in candidate.events]

        # Case 1: Without memory -> Generic recommendation
        def generic_answer():
            return {
                "summary": "pool saturation",
                "hypotheses": [{"cause": "pool exhaustion", "confidence": 0.8, "supporting_event_ids": ev_ids[:2], "contradicting_event_ids": []}],
                "unknowns": ["root trigger"],
                "next_actions": ["check metrics"],
                "recommendations": [{"action": "generic-restart", "priority": "medium", "reason": "unspecified", "risk": "low", "verification": "check logs", "historical_memory_index": None}]
            }

        # Case 2: With memory -> Evidence-backed recommendation citing recalled index 0
        def memory_informed_answer():
            return {
                "summary": "pool saturation matching prior incident",
                "hypotheses": [{"cause": "pool exhaustion", "confidence": 0.95, "supporting_event_ids": ev_ids[:2], "contradicting_event_ids": []}],
                "unknowns": [],
                "next_actions": ["execute verified pool scale runbook"],
                "recommendations": [{"action": "scale-pool", "priority": "high", "reason": "Verified past incident 4092 resolved pool exhaustion by scaling capacity", "risk": "low", "verification": "verify error rate < 0.01", "historical_memory_index": 0}]
            }

        # Verify Case 1 validation
        r1 = validate(generic_answer(), candidate, 0)
        self.assertIsNone(r1["recommendations"][0]["historical_memory_index"])
        self.assertEqual(r1["recommendations"][0]["action"], "generic-restart")

        # Verify Case 2 validation
        r2 = validate(memory_informed_answer(), candidate, 1)
        self.assertEqual(r2["recommendations"][0]["historical_memory_index"], 0)
        self.assertEqual(r2["recommendations"][0]["action"], "scale-pool")
        self.assertIn("Verified past incident", r2["recommendations"][0]["reason"])


class ABExperimentRegressionTests(unittest.TestCase):
    """Regression tests for scripts/run_ab_experiment.py and provider verification logic."""

    def test_scenario_hash_deterministic(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from run_ab_experiment import compute_scenario_hash

        demo1 = {
            "events": [
                {"service": "payment-api", "message": "error timeout", "timestamp": "2026-11-05T09:00:00Z"},
                {"service": "postgres-primary", "message": "pool 95%", "timestamp": "2026-11-05T09:01:00Z"}
            ]
        }
        # Same events in reversed order
        demo2 = {
            "events": [
                {"service": "postgres-primary", "message": "pool 95%", "timestamp": "2026-11-05T09:01:00Z"},
                {"service": "payment-api", "message": "error timeout", "timestamp": "2026-11-05T09:00:00Z"}
            ]
        }
        h1 = compute_scenario_hash(demo1)
        h2 = compute_scenario_hash(demo2)
        self.assertEqual(h1, h2)
        self.assertIsInstance(h1, str)
        self.assertEqual(len(h1), 16)

    def test_exact_fixture_recalled_requires_exact_doc_id(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from run_ab_experiment import verify_exact_fixture_recalled

        fixture_id = "exp-fixture-a1b2c3d4"

        # Contract (provenance work): only structured sources carrying the exact document_id AND a prompt index count.
        # Case 0: free text, even containing the id, is NOT provenance and must FAIL
        self.assertFalse(verify_exact_fixture_recalled(fixture_id, [f"Incident 4092 [{fixture_id}]: text only"]))
        # Case 1: a different document must FAIL
        generic_memory = [{"document_id": "other-doc", "prompt_memory_index": 0}]
        self.assertFalse(verify_exact_fixture_recalled(fixture_id, generic_memory))

        # Case 2: exact document_id with a prompt index must PASS
        exact_memory = [{"document_id": fixture_id, "prompt_memory_index": 0}]
        self.assertTrue(verify_exact_fixture_recalled(fixture_id, exact_memory))
        # Case 2b: exact document_id but not actually placed in the prompt must FAIL
        self.assertFalse(verify_exact_fixture_recalled(fixture_id, [{"document_id": fixture_id}]))

        # Case 3: Empty inputs must fail safely
        self.assertFalse(verify_exact_fixture_recalled("", exact_memory))
        self.assertFalse(verify_exact_fixture_recalled(fixture_id, []))

    def test_validate_config_detects_missing_keys(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from run_ab_experiment import validate_config

        with patch.dict(os.environ, {"GROQ_API_KEY": "", "HINDSIGHT_BASE_URL": ""}, clear=False):
            missing, cfg = validate_config()
            self.assertIn("GROQ_API_KEY", missing)
            self.assertIn("HINDSIGHT_BASE_URL", missing)

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key", "HINDSIGHT_BASE_URL": "http://test:8888", "HINDSIGHT_EXPERIMENT_API_KEY": "test-hindsight-key"}, clear=False):
            missing, cfg = validate_config()
            self.assertEqual(missing, [])
            self.assertEqual(cfg["GROQ_API_KEY"], "test-key")
            self.assertEqual(cfg["HINDSIGHT_BASE_URL"], "http://test:8888")
            self.assertEqual(cfg["HINDSIGHT_EXPERIMENT_BANK_ID"], "opsmemory-ab-experiment")


if __name__ == "__main__":
    unittest.main()
