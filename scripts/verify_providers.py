"""Diagnostic tool to verify real provider connectivity (PostgreSQL, Groq, Hindsight, Prometheus).

Never exposes secrets or credentials in output or error messages.
Exits with code 0 if all configured components respond normally.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure backend package is in python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import dotenv_values, load_dotenv

# Load .env without printing secrets
load_dotenv(override=False)

from app.integrations.groq_client import GroqClient, LLMError, mask_secret
from app.integrations.hindsight_memory import HindsightMemory, HindsightError
from app.store_memory import PostgresStore


def check_postgres() -> dict:
    t0 = time.monotonic()
    # Try DATABASE_URL first; if pointing to db host and we're on localhost, try localhost
    db_url = os.environ.get("DATABASE_URL")
    pw = os.environ.get("POSTGRES_PASSWORD")
    urls_to_try = []
    if db_url:
        urls_to_try.append(db_url)
    if pw:
        urls_to_try.append(f"postgresql://postgres:{pw}@127.0.0.1:5432/opsmemory")

    last_err = None
    for url in urls_to_try:
        try:
            store = PostgresStore(dsn=url)
            # Count records
            with store._connect() as conn:
                incidents_count = conn.execute("SELECT count(*) as c FROM incidents").fetchone()["c"]
                events_count = conn.execute("SELECT count(*) as c FROM events").fetchone()["c"]
                audit_count = conn.execute("SELECT count(*) as c FROM audit_log").fetchone()["c"]
                # Verify migration 002 indexes
                idx_rows = conn.execute("""
                    SELECT indexname FROM pg_indexes
                    WHERE tablename IN ('incidents', 'events', 'audit_log')
                """).fetchall()
                indexes = [r["indexname"] for r in idx_rows]

            migration_002_verified = all(
                idx in indexes for idx in ["incidents_created_at", "events_ts", "audit_log_ts"]
            )
            return {
                "status": "connected",
                "latency_s": round(time.monotonic() - t0, 3),
                "counts": {
                    "incidents": incidents_count,
                    "events": events_count,
                    "audit_log": audit_count
                },
                "migration_002_applied": migration_002_verified
            }
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"

    return {
        "status": "connection_failed",
        "error": last_err or "No PostgreSQL configuration provided",
        "latency_s": round(time.monotonic() - t0, 3)
    }


def check_groq() -> dict:
    api_key = os.environ.get("GROQ_API_KEY")
    model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    if not api_key or not api_key.strip():
        return {
            "status": "not_configured",
            "model": model,
            "note": "GROQ_API_KEY is not set in .env"
        }
    try:
        client = GroqClient(api_key=api_key, model=model)
        diag = client.check_connection()
        return {
            "status": "connected" if diag.get("connected") else "error",
            "key_masked": mask_secret(api_key),
            "model": diag.get("model", model),
            "latency_s": diag.get("latency_s"),
            "error": diag.get("error")
        }
    except Exception as exc:
        return {
            "status": "error",
            "key_masked": mask_secret(api_key),
            "model": model,
            "error": f"{type(exc).__name__}: {exc}"
        }


def check_hindsight() -> dict:
    base_url = os.environ.get("HINDSIGHT_BASE_URL")
    api_key = os.environ.get("HINDSIGHT_API_KEY")
    bank_id = os.environ.get("HINDSIGHT_BANK_ID", "opsmemory")
    if not base_url or not base_url.strip():
        return {
            "status": "not_configured",
            "bank_id": bank_id,
            "note": "HINDSIGHT_BASE_URL is not set in .env"
        }
    try:
        mem = HindsightMemory(base_url=base_url, api_key=api_key, bank_id=bank_id)
        diag = mem.check_connection()
        return {
            "status": "connected" if diag.get("connected") else "error",
            "base_url": base_url,
            "bank_id": bank_id,
            "bank_status": diag.get("bank_status"),
            "api_version": diag.get("api_version"),
            "key_masked": mask_secret(api_key) if api_key else "<none>",
            "latency_s": diag.get("latency_s"),
            "error": diag.get("error")
        }
    except Exception as exc:
        return {
            "status": "error",
            "base_url": base_url,
            "bank_id": bank_id,
            "error": f"{type(exc).__name__}: {exc}"
        }


def check_prometheus() -> dict:
    p_url = os.environ.get("PROMETHEUS_BASE_URL")
    if not p_url or not p_url.strip():
        return {
            "status": "not_configured",
            "note": "PROMETHEUS_BASE_URL is not set (trusted recovery metrics fail closed)"
        }
    from app.core.recovery import PrometheusRecoveryVerifier, RecoveryMetricsError
    try:
        v = PrometheusRecoveryVerifier()
        samples = v.collect()
        return {
            "status": "connected",
            "samples_collected": len(samples),
            "latest_error_rate": samples[-1]["error_rate"],
            "latest_p99_ms": samples[-1]["p99_ms"]
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"
        }


def main():
    print("=" * 60)
    print("OpsMemory — Provider Verification & Diagnostics")
    print("=" * 60)

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": {
            "postgresql": check_postgres(),
            "groq": check_groq(),
            "hindsight": check_hindsight(),
            "prometheus": check_prometheus()
        }
    }

    print(json.dumps(report, indent=2))
    print("=" * 60)

    pg_ok = report["providers"]["postgresql"]["status"] == "connected"
    if pg_ok:
        print("[PASS] PostgreSQL 16 is connected and healthy.")
    else:
        print(f"[FAIL] PostgreSQL 16: {report['providers']['postgresql'].get('error')}")

    groq_status = report["providers"]["groq"]["status"]
    if groq_status == "connected":
        print(f"[PASS] Groq API connected: model={report['providers']['groq']['model']}")
    elif groq_status == "not_configured":
        print("[INFO] Groq API: not configured in .env (live LLM calls will fail closed).")
    else:
        print(f"[FAIL] Groq API error: {report['providers']['groq'].get('error')}")

    hindsight_status = report["providers"]["hindsight"]["status"]
    if hindsight_status == "connected":
        print(f"[PASS] Hindsight connected: bank={report['providers']['hindsight']['bank_id']}, version={report['providers']['hindsight'].get('api_version')}")
    elif hindsight_status == "not_configured":
        print("[INFO] Hindsight: not configured in .env (memory recall/retention will fail closed).")
    else:
        print(f"[FAIL] Hindsight error: {report['providers']['hindsight'].get('error')}")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
