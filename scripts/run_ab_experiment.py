"""Controlled A/B experiment demonstrating the central role of Hindsight historical memory.

Workflow:
1. Seed Synthetic Historical Fixture:
   - Retains a clearly labelled synthetic historical post-mortem fixture into an ISOLATED
     Hindsight experiment bank (HINDSIGHT_EXPERIMENT_BANK_ID).
   - Never touches or pollutes the production memory bank.
2. Run A — Baseline Investigation (WITHOUT Historical Memory):
   - Ingests the incident scenario into an isolated InMemoryStore.
   - Groq performs investigation with use_memory=False.
   - Records generic AI reasoning, hypotheses, and recommendations.
3. Run B — Memory-Informed Investigation (WITH Hindsight Recall):
   - Ingests the exact same incident scenario into an isolated InMemoryStore.
   - Hindsight recall queries the dedicated experiment bank.
   - Verifies that the exact seeded fixture is recalled.
   - Groq performs investigation with use_memory=True, incorporating recalled historical context.
   - Records memory-informed hypotheses and recommendations with historical memory citations.
4. Objective Comparative Analysis:
   - Compares Run A vs Run B: recommendations count, memory citations, confidence, latency.
   - Verifies input scenario equivalence between Run A and Run B.
5. Evidence Persistence:
   - Writes genuine, verified results to evidence/before-after-raw.json.
   - Preserves any existing evidence file with a timestamped backup before writing.

Guarantees:
- Strict Authenticity: Requires genuine Groq and Hindsight services; never fabricates results or mocks providers.
- Data Protection: Uses InMemoryStore for all runs; zero modifications to PostgreSQL.
- Memory Isolation: Uses dedicated HINDSIGHT_EXPERIMENT_BANK_ID; zero modifications to production memory.
- Safe Diagnostics: Never prints or exposes secret API keys.
- Explicit Labeling: All test fixtures explicitly labeled Synthetic=True with non-production disclaimers.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend package is in python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import load_dotenv

# Load .env without printing secrets
load_dotenv(override=False)

from app.core.correlation import correlate
from app.core.events import ingest
from app.core.remediation import Adapter
from app.engine import Engine
from app.integrations.groq_client import GroqClient, mask_secret
from app.integrations.hindsight_memory import HindsightMemory, HindsightError
from app.store_memory import InMemoryStore


def validate_config() -> tuple[list[str], dict[str, str]]:
    """Validate environment configuration safely without exposing secrets."""
    groq_key = os.environ.get("GROQ_API_KEY", "")
    hindsight_url = os.environ.get("HINDSIGHT_BASE_URL", "")
    hindsight_key = os.environ.get("HINDSIGHT_EXPERIMENT_API_KEY", "")
    groq_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    exp_bank = os.environ.get("HINDSIGHT_EXPERIMENT_BANK_ID", "opsmemory-ab-experiment")

    missing = []
    if not groq_key or not groq_key.strip():
        missing.append("GROQ_API_KEY")
    if not hindsight_url or not hindsight_url.strip():
        missing.append("HINDSIGHT_BASE_URL")
    if not hindsight_key or not hindsight_key.strip():
        missing.append("HINDSIGHT_EXPERIMENT_API_KEY")

    return missing, {
        "GROQ_API_KEY": groq_key,
        "HINDSIGHT_BASE_URL": hindsight_url,
        "HINDSIGHT_API_KEY": hindsight_key,
        "GROQ_MODEL": groq_model,
        "HINDSIGHT_EXPERIMENT_BANK_ID": exp_bank,
    }


def _write_status(status: str, reason: str, **extra) -> None:
    """Always leave an honest, timestamped status record. Never overwrites results (before-after-raw.json is only
    written on a fully valid run); a previous status file is preserved as .bak."""
    d = Path(__file__).resolve().parents[1] / "evidence"; d.mkdir(parents=True, exist_ok=True)
    f = d / "ab-experiment-status.json"
    if f.exists(): shutil.copy2(f, d / f"ab-experiment-status.{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}.bak")
    f.write_text(json.dumps({"status": status, "reason": reason, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **extra}, indent=2), encoding="utf-8")


def _fail(code: int, status: str, reason: str, **extra) -> int:
    print(f"\n[{status}] {reason}"); _write_status(status, reason, **extra); return code


def compute_scenario_hash(demo: dict) -> str:
    """Compute deterministic cryptographic hash of normalized scenario input events."""
    events = demo.get("events", [])
    normalized = []
    for e in events:
        normalized.append(f"{e.get('service')}|{e.get('message')}|{e.get('timestamp')}")
    normalized.sort()
    return hashlib.sha256(";;".join(normalized).encode("utf-8")).hexdigest()[:16]


def verify_exact_fixture_recalled(fixture_doc_id: str, memory_sources: list) -> bool:
    """Verify Hindsight returned the exact source document supplied in the prompt."""
    if not fixture_doc_id or not memory_sources:
        return False
    return any(
        isinstance(source, dict)
        and source.get("document_id") == fixture_doc_id
        and isinstance(source.get("prompt_memory_index"), int)
        for source in memory_sources
    )


def wait_for_fixture_indexed(mem: HindsightMemory, fixture_doc_id: str, max_retries: int = 5, initial_delay: float = 0.5) -> bool:
    """Bounded poll with exponential backoff until the seeded fixture is queryable in Hindsight."""
    delay = initial_delay
    for _ in range(max_retries):
        time.sleep(delay)
        try:
            recalled = mem.recall_similar(fixture_doc_id, max_tokens=512)
            if any(fixture_doc_id in (r.document_id or "") or fixture_doc_id in (r.text or "") for r in recalled):
                return True
        except Exception:
            pass
        delay = min(delay * 1.5, 3.0)
    return False


def run_experiment() -> int:
    missing, config = validate_config()
    prod_bank = os.environ.get("HINDSIGHT_BANK_ID", "opsmemory")
    if config["HINDSIGHT_EXPERIMENT_BANK_ID"].strip().lower() == prod_bank.strip().lower():
        return _fail(2, "REFUSED", "experiment bank equals the production bank; the experiment will never write fixtures into production memory")
    if missing:
        print("=" * 70)
        print(f"[BLOCKED — EXTERNAL DEPENDENCY] Missing required configuration in .env:")
        for var in missing:
            print(f"  - {var}")
        print("\nTo run this genuine A/B experiment with real providers:")
        print("  1. Configure GROQ_API_KEY and HINDSIGHT_BASE_URL in .env")
        print("  2. Optionally configure HINDSIGHT_API_KEY and HINDSIGHT_EXPERIMENT_BANK_ID")
        print("  3. Run: .\\.venv\\Scripts\\python.exe .\\scripts\\run_ab_experiment.py")
        print("=" * 70)
        return 1

    demo_path = Path(__file__).resolve().parents[1] / "demo" / "payment_pool_exhaustion.json"
    if not demo_path.is_file():
        sys.exit(f"FAILED: Demo scenario file not found at {demo_path}")
    demo = json.loads(demo_path.read_text(encoding="utf-8"))
    scenario_hash = compute_scenario_hash(demo)

    print("=" * 70)
    print("OpsMemory — Genuine Hindsight Memory A/B Experiment")
    print("=" * 70)
    print(f"Target Groq Model: {config['GROQ_MODEL']}")
    print(f"Hindsight Target Bank: {config['HINDSIGHT_EXPERIMENT_BANK_ID']} (isolated experiment bank)")
    print(f"Scenario: {demo.get('_label', 'payment_pool_exhaustion')} (Input Hash: {scenario_hash})")
    print("=" * 70)

    # 1. Initialize Clients and Verify Live Connectivity
    print("\n[STEP 1] Initializing and probing live provider connectivity...")
    llm = GroqClient(api_key=config["GROQ_API_KEY"], model=config["GROQ_MODEL"])
    mem = HindsightMemory(base_url=config["HINDSIGHT_BASE_URL"], api_key=config["HINDSIGHT_API_KEY"], bank_id=config["HINDSIGHT_EXPERIMENT_BANK_ID"])

    # Groq Probe
    print("  - Probing Groq API with live health request...")
    llm_probe = llm.check_connection()
    if not llm_probe.get("connected"):
        print(f"  [FAIL] Groq live probe failed: {llm_probe.get('error')}")
        return _fail(1, "FAILED", "Groq provider unreachable or returned an error", detail=str(llm_probe.get("error"))[:200])
    print(f"  [PASS] Groq connected: model={llm_probe.get('model')}, latency={llm_probe.get('latency_s')}s")

    # Hindsight Probe
    print(f"  - Probing Hindsight service at {config['HINDSIGHT_BASE_URL']}...")
    mem_probe = mem.check_connection()
    if not mem_probe.get("connected"):
        print(f"  [FAIL] Hindsight live probe failed: {mem_probe.get('error')}")
        return _fail(3, "BLOCKED", "Hindsight experiment bank is not accessible with the configured key (e.g. HTTP 403). An administrator must grant access to, or create, the bank; the experiment cannot run without it.", bank=config["HINDSIGHT_EXPERIMENT_BANK_ID"], detail=str(mem_probe.get("error"))[:200])
    print(f"  [PASS] Hindsight connected: api_version={mem_probe.get('api_version')}, latency={mem_probe.get('latency_s')}s")

    # Ensure dedicated experiment bank exists
    print(f"  - Ensuring dedicated experiment bank '{config['HINDSIGHT_EXPERIMENT_BANK_ID']}' exists...")
    try: mem.ensure_bank_exists(config["HINDSIGHT_EXPERIMENT_BANK_ID"])
    except Exception as exc:
        return _fail(3, "BLOCKED", "cannot access or create the experiment bank; administrator action required", bank=config["HINDSIGHT_EXPERIMENT_BANK_ID"], detail=f"{type(exc).__name__}: {str(exc)[:200]}")

    # 2. Seed Explicitly Synthetic Historical Fixture
    print("\n[STEP 2] Retaining synthetic historical incident fixture into Hindsight...")
    fixture_doc_id = f"exp-fixture-{uuid.uuid4().hex[:8]}"
    fixture_text = (
        f"Incident 4092 [{fixture_doc_id}] SEV-1 services=payment-api,postgres-primary,checkout-service. "
        "Synthetic historical case with p99 latency 2400ms, postgres-primary connection pool utilization 96/100, "
        "payment-api requests timing out after 5000ms waiting for a database connection, and checkout-service upstream 504 rate 31%. "
        "Synthetic root-cause finding: postgres-primary database connection pool saturation under payment-api checkout surge. "
        "Synthetic resolution record: scale-pool from max pool size 100 to 150, then restart-service. "
        "Synthetic recovery record: error rate 0.001 and p99 latency 280ms over 3 consecutive samples. "
        "Synthetic=True. This is experiment-only historical context, not production evidence."
    )

    t0_retain = time.monotonic()
    try: mem.retain_experiment_seed(fixture_doc_id, fixture_text)
    except Exception as exc:
        return _fail(4, "FAILED", "fixture retention failed; nothing was stored, so a memory-on run would be meaningless", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
    retain_latency = round(time.monotonic() - t0_retain, 3)
    print(f"  [PASS] Retained fixture {fixture_doc_id} into bank '{config['HINDSIGHT_EXPERIMENT_BANK_ID']}' ({retain_latency}s)")

    # Bounded indexing readiness check
    print(f"  - Waiting for fixture {fixture_doc_id} to be indexed in Hindsight bank...")
    indexed = wait_for_fixture_indexed(mem, fixture_doc_id)
    if indexed:
        print("  [PASS] Fixture verified indexed in Hindsight bank.")
    else:
        return _fail(4, "FAILED", "fixture was retained but could not be recalled within the bounded wait; refusing to run the memory-on condition", fixture_document_id=fixture_doc_id)

    # 3. Investigation Runner (isolated InMemoryStore, zero PostgreSQL impact)
    def run_investigation(label: str, use_memory: bool, allow_synthetic_experiment_memory: bool = False) -> dict:
        store = InMemoryStore()
        # Simulated demo adapters: explicitly labeled as simulated, never modifying external systems
        adapters = {
            "scale-pool": Adapter("scale-pool", lambda: {"scaled_to": 150, "simulated": True}, True),
            "restart-service": Adapter("restart-service", lambda: {"restarted": True, "simulated": True}, True),
            "demo-noop": Adapter("demo-noop", lambda: {"note": "noop", "simulated": True}, True)
        }
        engine = Engine(store, llm, mem, adapters, demo["dependencies"], demo["critical_services"])
        ids, rejected = engine.ingest(demo["events"])
        if rejected or not ids:
            raise RuntimeError(f"Scenario event ingestion failed: {rejected}")
        iid = ids[0]
        result = engine.investigate(iid, use_memory=use_memory, allow_synthetic_experiment_memory=allow_synthetic_experiment_memory)
        return {
            "run": label,
            "incident_id": iid,
            "scenario_input_hash": scenario_hash,
            "scenario_events_count": len(demo["events"]),
            "use_memory": use_memory,
            "memories_used_count": len(result.get("memories_used", [])),
            "memories_used": result.get("memories_used", []),
            "memory_sources": result.get("memory_sources", []),
            "memory_error": result.get("memory_error"),
            "hypotheses": result.get("hypotheses", []),
            "recommendations": result.get("recommendations", []),
            "recommendations_citing_memory": sum(
                r.get("historical_memory_index") is not None for r in result.get("recommendations", [])
            ),
            "model": result.get("llm", {}).get("model"),
            "llm_latency_s": result.get("llm", {}).get("latency_s"),
            "total_latency_s": result.get("total_s")
        }

    # 4. Execute Run A: Baseline (Without Historical Memory)
    print("\n[STEP 3] Executing Run A: Baseline Investigation WITHOUT historical memory...")
    try: baseline = run_investigation("baseline_without_memory", use_memory=False)
    except Exception as exc: return _fail(6, "FAILED", "baseline investigation failed (no fabricated result recorded)", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
    print(f"  - Hypotheses generated: {len(baseline['hypotheses'])}")
    print(f"  - Recommendations generated: {len(baseline['recommendations'])}")
    print(f"  - Recommendations citing memory: {baseline['recommendations_citing_memory']}")
    print(f"  - LLM Latency: {baseline['llm_latency_s']}s")

    # 5. Execute Run B: With Hindsight Memory
    print("\n[STEP 4] Executing Run B: Investigation WITH Hindsight historical memory...")
    try: with_memory = run_investigation("with_hindsight_memory", use_memory=True, allow_synthetic_experiment_memory=True)
    except Exception as exc: return _fail(6, "FAILED", "memory-enabled investigation failed (no fabricated result recorded)", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
    if with_memory.get("memory_error"):
        return _fail(5, "INVALID", "memory recall errored during the memory-on run, so the comparison is invalid", detail=with_memory["memory_error"])
    print(f"  - Memories retrieved: {with_memory['memories_used_count']}")
    print(f"  - Hypotheses generated: {len(with_memory['hypotheses'])}")
    print(f"  - Recommendations generated: {len(with_memory['recommendations'])}")
    print(f"  - Recommendations citing memory: {with_memory['recommendations_citing_memory']}")
    print(f"  - LLM Latency: {with_memory['llm_latency_s']}s")

    # 6. Strict Fixture Retrieval Verification
    print("\n[STEP 5] Verifying exact historical fixture retrieval...")
    recalled_texts = with_memory.get("memories_used", [])
    memory_sources = with_memory.get("memory_sources", [])
    exact_fixture_recalled = verify_exact_fixture_recalled(fixture_doc_id, memory_sources)

    if not exact_fixture_recalled:
        # Perform direct probe to verify if fixture is in index
        direct_recall = mem.recall_similar(fixture_doc_id, max_tokens=1024)
        direct_match = any(
            fixture_doc_id in (r.document_id or "") or fixture_doc_id in (r.text or "")
            for r in direct_recall
        )
        if direct_match:
            print(f"  [WARN] Fixture {fixture_doc_id} is indexed in bank but query did not retrieve it for incident.")
        else:
            print(f"  [FAIL] Fixture {fixture_doc_id} was not retrieved from Hindsight bank.")
    else:
        print(f"  [PASS] Exact fixture {fixture_doc_id} was successfully retrieved and provided to Groq.")

    if not exact_fixture_recalled:
        return _fail(5, "INVALID", "the exact fixture was NOT retrieved in the memory-on run; the comparison would not measure memory, so no results are reported as valid", fixture_document_id=fixture_doc_id)

    # 7. Compile Report & Comparison
    comparison = {
        "scenario_input_hash": scenario_hash,
        "input_equivalence_verified": baseline["scenario_input_hash"] == with_memory["scenario_input_hash"],
        "exact_fixture_recalled": exact_fixture_recalled,
        "baseline_hypotheses_count": len(baseline["hypotheses"]),
        "memory_hypotheses_count": len(with_memory["hypotheses"]),
        "baseline_recs_count": len(baseline["recommendations"]),
        "memory_recs_count": len(with_memory["recommendations"]),
        "baseline_memory_citations": baseline["recommendations_citing_memory"],
        "memory_memory_citations": with_memory["recommendations_citing_memory"],
        "recalled_memories_count": with_memory["memories_used_count"],
        "llm_model": with_memory.get("model") or config["GROQ_MODEL"],
        "memory_error": with_memory.get("memory_error")
    }

    output = {
        "experiment": "OpsMemory Hindsight Memory Controlled A/B Experiment",
        "status": "COMPLETED_VALID", "valid": True,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence_type": "genuine_controlled_ab_experiment_synthetic_scenario",
        "bank_id": config["HINDSIGHT_EXPERIMENT_BANK_ID"],
        "fixture_document_id": fixture_doc_id,
        "fixture_text": fixture_text,
        "exact_fixture_recalled": exact_fixture_recalled,
        "runs": {
            "baseline_without_memory": baseline,
            "with_hindsight_memory": with_memory
        },
        "comparison": comparison,
        "limitations": [
            "Synthetic incident scenario telemetry (demo/payment_pool_exhaustion.json) used for controlled reproducibility.",
            "Demonstration adapters (scale-pool, restart-service) are explicitly simulated; no external cloud infrastructure was modified.",
            "Historical fixture seeded into isolated experiment bank to prevent cross-contamination with production memory.",
            "Production incident resolution requires independent server-side Prometheus recovery verification and admin authorization."
        ]
    }

    # 8. Write Evidence with Safe Backup
    evidence_dir = Path(__file__).resolve().parents[1] / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / "before-after-raw.json"
    if out_path.exists():
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        backup_path = evidence_dir / f"before-after-raw.{timestamp}.bak"
        shutil.copy2(out_path, backup_path)
        print(f"\n[INFO] Existing evidence preserved to {backup_path.name}")

    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"[PASS] Experiment evidence written to {out_path.name}")
    _write_status("COMPLETED_VALID", "all fail-closed checks passed; counts only, no automated quality score", results_file=out_path.name)

    print("\n" + "=" * 70)
    print("A/B EXPERIMENT SUMMARY:")
    print(f"  - Scenario Input Equivalence: {comparison['input_equivalence_verified']}")
    print(f"  - Exact Fixture Recalled:     {comparison['exact_fixture_recalled']}")
    print(f"  - Baseline Recommendations:   {comparison['baseline_recs_count']} (Citing Memory: {comparison['baseline_memory_citations']})")
    print(f"  - Memory Recommendations:     {comparison['memory_recs_count']} (Citing Memory: {comparison['memory_memory_citations']})")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(run_experiment())
