"""End-to-end integration tests for complete incident lifecycle with PostgreSQL 16 persistence.

Tests verify real transactional DB persistence, event correlation, anti-fabrication
investigation validation, RBAC human approval, controlled remediation adapter execution,
recovery verification, post-mortem generation, and Hindsight memory retention gating.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Ensure 'backend' directory is on sys.path
backend_dir = str(Path(__file__).resolve().parents[2])
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from dotenv import dotenv_values

from app.core.correlation import correlate
from app.core.events import ingest, NormalizedEvent
from app.core.investigation import validate, build_prompt
from app.core.postmortem import generate
from app.core.remediation import (
    Adapter, ApprovalRequest, AuthorizationError, VerificationCriteria,
    decide, execute, verify_recovery
)
from app.core.severity import assess
from app.engine import Engine
from app.integrations.hindsight_memory import Memory
from app.store_memory import PostgresStore


class FullPostgresWorkflowIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fail closed: integration tests require an explicitly supplied disposable DB.
        dsn = os.environ.get("OPSMEMORY_TEST_DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "Set OPSMEMORY_TEST_DATABASE_URL to the disposable PostgreSQL test database; "
                "refusing DATABASE_URL/.env fallback."
            )
        parsed = urlsplit(dsn)
        if (
            parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 55432
            or parsed.path.lstrip("/") != "opsmemory_cleanstart"
        ):
            raise RuntimeError(
                "Unsafe integration-test DSN. Expected localhost:55432/opsmemory_cleanstart; "
                "refusing to connect."
            )
        cls.dsn = dsn
        try:
            cls.store = PostgresStore(dsn=cls.dsn)
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL 16 not reachable at {cls.dsn}: {exc}")

    def test_complete_incident_lifecycle_with_real_postgres(self):
        # 1. Prepare realistic multi-service incident batch
        batch_id = uuid.uuid4().hex[:8]
        ts_now = datetime.now(timezone.utc).isoformat()
        raw_events = [
            {
                "event_id": f"evt-{batch_id}-01",
                "timestamp": ts_now,
                "service": "postgres-pool",
                "environment": "prod",
                "severity": "critical",
                "source": "db-monitor",
                "message": "database connection pool exhausted: 100/100 connections active, 42 waiting",
                "metadata": {
                    "pool_size": 100,
                    "waiting_clients": 42,
                    "admin_token": "secret-super-token-12345"  # must be redacted
                }
            },
            {
                "event_id": f"evt-{batch_id}-02",
                "timestamp": ts_now,
                "service": "payment-service",
                "environment": "prod",
                "severity": "error",
                "source": "app-server",
                "message": "checkout payment failed: connection acquisition timeout after 5000ms",
                "metadata": {"error_code": "PG_ACQUIRE_TIMEOUT", "client_email": "customer@example.com"}
            },
            {
                "event_id": f"evt-{batch_id}-03",
                "timestamp": ts_now,
                "service": "checkout-service",
                "environment": "prod",
                "severity": "warning",
                "source": "api-gateway",
                "message": "checkout response latency spike: p99 exceeds 4500ms on /pay endpoint",
                "metadata": {"endpoint": "/pay", "p99_latency_ms": 4500}
            }
        ]

        deps = {"checkout-service": ["payment-service"], "payment-service": ["postgres-pool"]}
        critical = {"payment-service", "checkout-service"}

        # Mock memory that tracks retained memories
        retained_memories = []
        class RecordingMemory:
            def recall_similar(self, query):
                return [
                    Memory(
                        text="[VERIFIED post-mortem] Incident prev-42 sev-1. Proposed root cause (unconfirmed): pool exhaustion. Resolution: scaled max_connections to 150.",
                        type="experience",
                        id="hist-001",
                        metadata={"verified_metadata": "true", "outcome": "verified_recovery"}
                    )
                ]
            def retain_post_mortem(self, iid, text, *, verified, resolved_by=None):
                retained_memories.append({"iid": iid, "text": text, "verified": verified})
                return {"id": "retained-1", "status": "stored"}

        memory = RecordingMemory()

        # Dynamic mock LLM answering with real event IDs from the candidate
        class GroundedLLM:
            def __init__(self):
                self.target_ids = []
            def chat_json(self, system, user):
                assert "secret-super-token-12345" not in user, "Secret token leaked into LLM prompt!"
                assert "customer@example.com" not in user, "Email leaked into LLM prompt!"
                assert "HISTORICAL MEMORY" in user, "Historical memory missing from prompt!"
                return {
                    "summary": "Database connection pool exhaustion caused downstream payment failures and checkout latency.",
                    "hypotheses": [
                        {
                            "cause": "postgres-pool saturation starved payment-service workers",
                            "confidence": 0.92,
                            "supporting_event_ids": self.target_ids[:2],
                            "contradicting_event_ids": []
                        }
                    ],
                    "unknowns": ["What triggered the sudden connection surge?"],
                    "next_actions": ["Scale pool capacity", "Check slow transactions"],
                    "recommendations": [
                        {
                            "action": "scale-pool",
                            "priority": "high",
                            "reason": "Previous verified incident resolved identical symptoms by scaling pool capacity.",
                            "risk": "Low; database server has available memory",
                            "verification": "Verify error rate drops below 1% and p99 latency normalizes",
                            "historical_memory_index": 0
                        }
                    ]
                }, {"model": "openai/gpt-oss-120b", "latency_s": 0.25}

        llm = GroundedLLM()
        adapters = {
            "scale-pool": Adapter("scale-pool", lambda: {"scaled_to": 150, "status": "active"}, simulated=True),
            "demo-noop": Adapter("demo-noop", lambda: {"note": "noop"}, simulated=True)
        }

        engine = Engine(self.store, llm, memory, adapters, dependencies=deps, critical_services=critical)

        # Step 1: Ingestion and PostgreSQL Persistence
        ids, rejected = engine.ingest(raw_events)
        self.assertEqual(len(ids), 1, "Expected exactly 1 correlated incident")
        self.assertEqual(len(rejected), 0, "No events should be rejected")
        iid = ids[0]
        llm.target_ids = [e.event_id for e in engine._candidate(iid).events]

        # Verify idempotency: re-ingesting exact same batch is silently idempotent (accepted=[], rejected=[])
        ids2, rejected2 = engine.ingest(raw_events)
        self.assertEqual(ids2, [], "Duplicate batch should not generate new incident")
        self.assertEqual(len(rejected2), 0, "Silently idempotent: no validation errors on duplicate batch")

        # Step 2: Verify PostgreSQL state & Recursive Redaction
        db_inc = self.store.get_incident(iid)
        self.assertEqual(db_inc["id"], iid)
        self.assertEqual(db_inc["severity"], "SEV-2")
        self.assertEqual(db_inc["state"], "open")
        self.assertIn("payment-service", db_inc["services"])

        events_in_db = self.store.events_for_incident(iid)
        self.assertEqual(len(events_in_db), 3)
        # Verify secret token and email are redacted in database JSONB
        raw_db_json = json.dumps(events_in_db)
        self.assertNotIn("secret-super-token-12345", raw_db_json)
        self.assertNotIn("customer@example.com", raw_db_json)
        self.assertIn("[REDACTED]", raw_db_json)
        self.assertIn("[REDACTED_EMAIL]", raw_db_json)

        # Step 3: Investigation with Hindsight Recall & LLM Grounding
        investigation = engine.investigate(iid, use_memory=True)
        self.assertEqual(investigation["status"], "hypothesis_only")
        self.assertEqual(len(investigation["hypotheses"]), 1)
        self.assertEqual(investigation["hypotheses"][0]["confidence"], 0.92)
        self.assertEqual(len(investigation["memories_used"]), 1)
        self.assertIn("Proposed root cause (unconfirmed): pool exhaustion", investigation["memories_used"][0])
        self.assertEqual(investigation["recommendations"][0]["historical_memory_index"], 0)

        # Verify incident updated in PostgreSQL
        db_inc = self.store.get_incident(iid)
        self.assertEqual(db_inc["state"], "investigated")

        # Step 4: Human Approval Workflow with Role-based Access Control
        req = engine.request_remediation(iid, rec_index=0, adapter="scale-pool")
        self.assertEqual(req.state, "pending")
        self.assertEqual(req.action, "scale-pool")

        # Verify unauthorized user (viewer) is denied and audit entry is written
        with self.assertRaises(AuthorizationError):
            engine.decide(req.id, user="bob-viewer", role="viewer", approve=True)

        # Verify SRE operator approves
        engine.decide(req.id, user="alice-sre", role="sre", approve=True, reason="Symptoms match prior pool exhaustion")
        appr_db = self.store.get_approval(req.id)
        self.assertEqual(appr_db.state, "approved")

        # Step 5: Controlled Remediation Execution
        executed_req = engine.execute(req.id, user="alice-sre")
        self.assertEqual(executed_req.state, "executed")
        self.assertTrue(executed_req.result["simulated"])
        self.assertEqual(executed_req.result["scaled_to"], 150)

        # Step 6: Recovery Verification & Post-Mortem Generation
        recovery_samples = [
            {"timestamp": time.time() - 30, "error_rate": 0.002, "p99_ms": 320.0, "request_count": 5, "degraded": 0},
            {"timestamp": time.time() - 15, "error_rate": 0.001, "p99_ms": 310.0, "request_count": 5, "degraded": 0},
            {"timestamp": time.time(), "error_rate": 0.0008, "p99_ms": 290.0, "request_count": 5, "degraded": 0}
        ]
        pm, retained = engine.verify_and_close(iid, recovery_samples, synthetic=False)
        self.assertTrue(pm["verification"]["resolved"])
        self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")
        self.assertEqual(len(pm["resolution"]), 1)
        # Simulated remediation must never become trusted Hindsight learning.
        self.assertIsNone(retained)
        self.assertEqual(pm["memory_retention"]["status"], "not_applicable")
        self.assertEqual(len(retained_memories), 0)
        self.assertFalse(pm["retain_to_hindsight"])

        # Verify final state in PostgreSQL
        final_inc = self.store.get_incident(iid)
        self.assertEqual(final_inc["state"], "resolved")

        # Step 7: Restart Durability Verification
        # Create brand-new engine from DB and verify candidate reloads correctly
        fresh_engine = Engine(self.store, GroundedLLM(), memory, adapters, dependencies=deps, critical_services=critical)
        reloaded_candidate = fresh_engine._candidate(iid)
        self.assertEqual(len(reloaded_candidate.events), 3)
        self.assertEqual(set(reloaded_candidate.services), {"checkout-service", "payment-service", "postgres-pool"})

        # Step 8: Audit Log Verification in PostgreSQL
        audit_entries = self.store.audit_log_entries(limit=10)
        events_logged = [e["event"] for e in audit_entries]
        self.assertIn("approval_denied", events_logged)
        self.assertIn("approval_decision", events_logged)
        self.assertIn("remediation_execution", events_logged)


if __name__ == "__main__":
    unittest.main()
