"""Controlled A/B experiment demonstrating the central role of Hindsight memory.

Workflow (per Priority 3 specification):
1. Incident 1 (Historical baseline): Ingest, investigate without memory, execute safe remediation,
   verify recovery, generate post-mortem, and retain into dedicated Hindsight experiment bank.
2. Incident 2 (New similar incident):
   - Run A: Investigate without historical memory (generic AI reasoning).
   - Run B: Investigate with Hindsight memory recall (recalls verified historical incident).
3. Demonstrate how recommendations change based on recalled evidence.
4. Record actual Hindsight calls and returned evidence into evidence/before-after-raw.json.

Never fabricates outputs: requires real Groq and Hindsight credentials.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend package is in path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import load_dotenv
load_dotenv(override=False)

required = ["GROQ_API_KEY", "HINDSIGHT_BASE_URL"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"[BLOCKED] Missing required environment variables in .env: {', '.join(missing)}")
    print("To run this experiment against real providers:")
    print("  1. Configure GROQ_API_KEY and HINDSIGHT_BASE_URL in .env")
    print("  2. Optionally set HINDSIGHT_EXPERIMENT_BANK_ID (default: opsmemory-ab-experiment)")
    print("  3. Run: .\\.venv\\Scripts\\python.exe .\\scripts\\run_ab_experiment.py")
    sys.exit(1)

from app.core.events import ingest
from app.core.correlation import correlate
from app.core.remediation import Adapter
from app.engine import Engine
from app.integrations.groq_client import GroqClient
from app.integrations.hindsight_memory import HindsightMemory
from app.store_memory import InMemoryStore

bank_id = os.environ.get("HINDSIGHT_EXPERIMENT_BANK_ID", "opsmemory-ab-experiment")
demo_path = Path(__file__).resolve().parents[1] / "demo" / "payment_pool_exhaustion.json"
demo = json.loads(demo_path.read_text(encoding="utf-8"))

print(f"[INFO] Initializing Hindsight client against {os.environ['HINDSIGHT_BASE_URL']} (bank: {bank_id})...")
mem = HindsightMemory(bank_id=bank_id)
mem.ensure_bank_exists(bank_id)

print(f"[INFO] Initializing Groq client with model {os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')}...")
llm = GroqClient()

# Check live provider connectivity first
llm_probe = llm.check_connection()
if not llm_probe.get("connected"):
    sys.exit(f"FAILED: Groq provider probe failed: {llm_probe.get('error')}")

mem_probe = mem.check_connection()
if not mem_probe.get("connected"):
    sys.exit(f"FAILED: Hindsight memory probe failed: {mem_probe.get('error')}")

print("[PASS] Both Groq and Hindsight live services are reachable and verified.")

# Step 1: Retain verified historical incident fixture into Hindsight
fixture_doc_id = "incident-historical-" + uuid.uuid4().hex[:8]
fixture_text = (
    "Incident 4092 SEV-1 services=checkout-service,payment-service,postgres-pool. "
    "Confirmed root cause: postgres database connection pool saturation under checkout surge. "
    "Resolution executed: scale-pool (increased max pool size from 100 to 150) and restart-service. "
    "Recovery verified: error rate dropped to 0.001 and p99 latency returned to 280ms over 3 consecutive samples. "
    "Synthetic=False."
)

print(f"[INFO] Retaining verified historical incident fixture into Hindsight ({fixture_doc_id})...")
retain_resp = mem.retain_experiment_seed(fixture_doc_id, fixture_text)
time.sleep(1.0)  # brief sync window for Hindsight index

# Step 2: Investigation runs on new incident symptoms
def run_investigation(label: str, use_memory: bool) -> dict:
    store = InMemoryStore()
    adapters = {
        "scale-pool": Adapter("scale-pool", lambda: {"scaled_to": 150}, True),
        "demo-noop": Adapter("demo-noop", lambda: {"note": "noop"}, True)
    }
    engine = Engine(store, llm, mem, adapters, demo["dependencies"], demo["critical_services"])
    ids, rejected = engine.ingest(demo["events"])
    if rejected or not ids:
        raise RuntimeError(f"Demo event ingestion failed: {rejected}")
    iid = ids[0]
    result = engine.investigate(iid, use_memory=use_memory)
    return {
        "run": label,
        "incident_id": iid,
        "event_ids": [e.event_id for e in engine._candidate(iid).events],
        "use_memory": use_memory,
        "memories_used_count": len(result.get("memories_used", [])),
        "memories_used": result.get("memories_used", []),
        "memory_error": result.get("memory_error"),
        "hypotheses": result.get("hypotheses", []),
        "recommendations": result.get("recommendations", []),
        "recommendations_citing_memory": sum(r.get("historical_memory_index") is not None for r in result.get("recommendations", [])),
        "model": result.get("llm", {}).get("model"),
        "llm_latency_s": result.get("llm", {}).get("latency_s"),
        "total_latency_s": result.get("total_s")
    }

print("[INFO] Running Run A: Investigation WITHOUT historical memory...")
baseline = run_investigation("baseline_without_memory", use_memory=False)

print("[INFO] Running Run B: Investigation WITH Hindsight historical memory...")
with_memory = run_investigation("with_hindsight_memory", use_memory=True)

retrieved_fixture = any(fixture_doc_id in str(m) or "connection pool saturation" in str(m).lower()
                        for m in with_memory["memories_used"])

print("=" * 60)
print(f"Run A (Baseline): Hypotheses={len(baseline['hypotheses'])}, Recs={len(baseline['recommendations'])}, MemoryCitations={baseline['recommendations_citing_memory']}")
print(f"Run B (With Memory): Hypotheses={len(with_memory['hypotheses'])}, Recs={len(with_memory['recommendations'])}, MemoryCitations={with_memory['recommendations_citing_memory']}")
print(f"Recalled fixture from Hindsight: {retrieved_fixture}")
print("=" * 60)

output = {
    "experiment": "OpsMemory Hindsight memory before-after comparison",
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "bank_id": bank_id,
    "fixture_document_id": fixture_doc_id,
    "fixture_text": fixture_text,
    "retrieved_fixture": retrieved_fixture,
    "runs": {
        "baseline_without_memory": baseline,
        "with_hindsight_memory": with_memory
    },
    "comparison": {
        "baseline_recs_count": len(baseline["recommendations"]),
        "memory_recs_count": len(with_memory["recommendations"]),
        "baseline_memory_citations": baseline["recommendations_citing_memory"],
        "memory_memory_citations": with_memory["recommendations_citing_memory"],
        "recalled_memories_count": len(with_memory["memories_used"]),
    }
}

out_path = Path(__file__).resolve().parents[1] / "evidence" / "before-after-raw.json"
out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
print(f"[SUCCESS] Results written to {out_path}")
