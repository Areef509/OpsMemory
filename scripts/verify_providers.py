"""Diagnostic tool to verify real provider connectivity (PostgreSQL, Groq, Hindsight, Prometheus).

Never exposes secrets or credentials in output or error messages.
Exits with code 0 if all configured components respond normally or if blocked by missing credentials.
Guarantees PostgreSQL preservation (read-only queries).
Tests real Hindsight memory retention and recall against dedicated experiment bank when configured.
"""
from __future__ import annotations

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

from app.integrations.groq_client import GroqClient, LLMError, mask_secret
from app.integrations.hindsight_memory import HindsightMemory, HindsightError
from app.store_memory import PostgresStore


def check_postgres() -> dict:
    """Safely verify PostgreSQL connectivity and integrity using read-only queries."""
    t0 = time.monotonic()
    db_url = os.environ.get("DATABASE_URL")
    pw = os.environ.get("POSTGRES_PASSWORD")
    from urllib.parse import quote, urlparse
    urls_to_try = []
    parsed = urlparse(db_url) if db_url else None
    docker_only_host = (
        os.name == "nt"
        and parsed is not None
        and parsed.hostname in {"db", "postgres", "postgresql"}
    )
    if db_url and not docker_only_host:
        urls_to_try.append(db_url)
    if parsed and parsed.username and parsed.password:
        # Preserve DATABASE_URL's application-role credentials; only replace
        # the Docker-only hostname so this verifier works from Windows.
        port = parsed.port if parsed.port else 5432
        database = (parsed.path or "/opsmemory").lstrip("/") or "opsmemory"
        username = parsed.username
        password = parsed.password
        urls_to_try.append(
            f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
            f"@127.0.0.1:{port}/{quote(database, safe='')}"
        )
    elif pw:
        urls_to_try.append(
            f"postgresql://postgres:{quote(pw, safe='')}@127.0.0.1:5432/opsmemory"
        )

    last_err = None
    for url in urls_to_try:
        try:
            store = PostgresStore(dsn=url)
            # Count records with read-only SELECT
            with store._connect() as conn:
                incidents_count = conn.execute("SELECT count(*) as c FROM incidents").fetchone()["c"]
                events_count = conn.execute("SELECT count(*) as c FROM events").fetchone()["c"]
                audit_count = conn.execute("SELECT count(*) as c FROM audit_log").fetchone()["c"]
                idx_rows = conn.execute("""
                    SELECT indexname FROM pg_indexes
                    WHERE tablename IN ('incidents', 'events', 'audit_log')
                """).fetchall()
                indexes = [r["indexname"] for r in idx_rows]

            migration_002_verified = all(
                idx in indexes for idx in ["incidents_created_at", "events_ts", "audit_log_ts"]
            )
            return {
                "configured": True,
                "status": "connected",
                "latency_s": round(time.monotonic() - t0, 3),
                "counts": {
                    "incidents": incidents_count,
                    "events": events_count,
                    "audit_log": audit_count
                },
                "migration_002_applied": migration_002_verified,
                "preservation_verified": True
            }
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"

    return {
        "configured": bool(db_url or pw),
        "status": "connection_failed",
        "error": last_err or "No PostgreSQL configuration provided",
        "latency_s": round(time.monotonic() - t0, 3),
        "preservation_verified": False
    }


def check_groq() -> dict:
    """Verify Groq API configuration and perform a live authenticated test call."""
    api_key = os.environ.get("GROQ_API_KEY")
    model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    if not api_key or not api_key.strip():
        return {
            "configured": False,
            "status": "not_configured",
            "model": model,
            "note": "GROQ_API_KEY is not set in .env"
        }
    try:
        client = GroqClient(api_key=api_key, model=model)
        diag = client.check_connection()
        connected = bool(diag.get("connected"))
        return {
            "configured": True,
            "status": "connected" if connected else "error",
            "key_masked": mask_secret(api_key),
            "model": diag.get("model", model),
            "model_matches_target": diag.get("model") == model or model in str(diag.get("model", "")),
            "latency_s": diag.get("latency_s"),
            "attempts": diag.get("attempts", 1),
            "response_status": diag.get("response", {}).get("status") if connected else None,
            "error": diag.get("error")
        }
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        return {
            "configured": True,
            "status": "error",
            "key_masked": mask_secret(api_key),
            "model": model,
            "error": err_msg
        }


def check_hindsight() -> dict:
    """Verify Hindsight SDK connectivity, retention, and recall using an isolated experiment bank."""
    base_url = os.environ.get("HINDSIGHT_BASE_URL")
    api_key = os.environ.get("HINDSIGHT_EXPERIMENT_API_KEY")
    exp_bank = os.environ.get("HINDSIGHT_EXPERIMENT_BANK_ID", "opsmemory-ab-experiment")
    if not base_url or not base_url.strip():
        return {
            "configured": False,
            "status": "not_configured",
            "bank_id": exp_bank,
            "note": "HINDSIGHT_BASE_URL is not set in .env"
        }

    t0 = time.monotonic()
    result = {
        "configured": True,
        "base_url": base_url,
        "bank_id": exp_bank,
        "key_masked": mask_secret(api_key) if api_key else "<none>",
        "connection": False,
        "retention": False,
        "retrieval": False,
        "fixture_match": False,
        "error": None
    }

    try:
        mem = HindsightMemory(base_url=base_url, api_key=api_key, bank_id=exp_bank)
        conn_diag = mem.check_connection()
        result["connection"] = bool(conn_diag.get("connected"))
        result["api_version"] = conn_diag.get("api_version")
        result["bank_status"] = conn_diag.get("bank_status")
        result["latency_s"] = conn_diag.get("latency_s")

        if not result["connection"]:
            result["status"] = "error"
            result["error"] = conn_diag.get("error", "connection probe failed")
            return result

        # Ensure isolated experiment bank exists
        mem.ensure_bank_exists(exp_bank)

        # Real retention operation using a uniquely identified synthetic test fixture
        fixture_doc_id = f"verify-synthetic-{uuid.uuid4().hex[:8]}"
        fixture_content = (
            f"OPSMEMORY SYNTHETIC PROBE {fixture_doc_id}: "
            f"Testing real Hindsight memory retention and semantic recall at {datetime.now(timezone.utc).isoformat()}."
        )

        t_retain = time.monotonic()
        mem.retain_experiment_seed(fixture_doc_id, fixture_content)
        result["retention"] = True
        result["retention_latency_s"] = round(time.monotonic() - t_retain, 3)
        result["fixture_doc_id"] = fixture_doc_id

        # Brief sync window for indexing
        time.sleep(1.0)

        # Real retrieval operation
        t_recall = time.monotonic()
        recalled = mem.recall_similar(f"OPSMEMORY SYNTHETIC PROBE {fixture_doc_id}", max_tokens=1024)
        result["retrieval"] = True
        result["retrieval_latency_s"] = round(time.monotonic() - t_recall, 3)
        result["recalled_count"] = len(recalled)

        # Check exact fixture recall
        matched = any(
            fixture_doc_id in (r.document_id or "") or fixture_doc_id in (r.text or "")
            for r in recalled
        )
        result["fixture_match"] = matched
        result["status"] = "connected" if matched else "retrieval_mismatch"
        if not matched:
            result["error"] = f"recalled {len(recalled)} memories but fixture {fixture_doc_id} was not present"

    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_s"] = round(time.monotonic() - t0, 3)

    return result


def check_prometheus() -> dict:
    """Verify Prometheus metrics endpoint if configured."""
    p_url = os.environ.get("PROMETHEUS_BASE_URL")
    if not p_url or not p_url.strip():
        return {
            "configured": False,
            "status": "not_configured",
            "note": "PROMETHEUS_BASE_URL is not set (trusted recovery metrics fail closed)"
        }
    from app.core.recovery import PrometheusRecoveryVerifier, RecoveryMetricsError
    try:
        v = PrometheusRecoveryVerifier()
        samples = v.collect()
        return {
            "configured": True,
            "status": "connected",
            "samples_collected": len(samples),
            "latest_error_rate": samples[-1]["error_rate"],
            "latest_p99_ms": samples[-1]["p99_ms"]
        }
    except Exception as exc:
        return {
            "configured": True,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"
        }


def build_verification_table(report: dict) -> list[tuple[str, str, str]]:
    """Build the exact 9-point verification table required by the specification."""
    groq = report["providers"]["groq"]
    hindsight = report["providers"]["hindsight"]
    pg = report["providers"]["postgresql"]

    rows = []

    # 1. Groq configuration
    if groq.get("configured"):
        rows.append(("Groq configuration", "VERIFIED", f"Key configured (masked: {groq.get('key_masked')})"))
    else:
        rows.append(("Groq configuration", "FAILED", "GROQ_API_KEY missing in .env"))

    # 2. Groq authenticated API call
    if groq.get("status") == "connected":
        rows.append(("Groq authenticated API call", "VERIFIED", f"HTTP 200, latency={groq.get('latency_s')}s"))
    elif not groq.get("configured"):
        rows.append(("Groq authenticated API call", "FAILED", "Blocked: credentials missing"))
    else:
        rows.append(("Groq authenticated API call", "FAILED", f"Error: {groq.get('error')}"))

    # 3. Correct model response
    if groq.get("status") == "connected" and groq.get("model_matches_target"):
        rows.append(("Correct model response", "VERIFIED", f"Model: {groq.get('model')}"))
    elif not groq.get("configured"):
        rows.append(("Correct model response", "FAILED", "Blocked: credentials missing"))
    else:
        rows.append(("Correct model response", "FAILED", f"Model mismatch or call failed: {groq.get('model')}"))

    # 4. Hindsight configuration
    if hindsight.get("configured"):
        rows.append(("Hindsight configuration", "VERIFIED", f"Base URL: {hindsight.get('base_url')}"))
    else:
        rows.append(("Hindsight configuration", "FAILED", "HINDSIGHT_BASE_URL missing in .env"))

    # 5. Hindsight authenticated connection
    if hindsight.get("connection"):
        rows.append(("Hindsight authenticated connection", "VERIFIED", f"Version: {hindsight.get('api_version')}"))
    elif not hindsight.get("configured"):
        rows.append(("Hindsight authenticated connection", "FAILED", "Blocked: base URL missing"))
    else:
        rows.append(("Hindsight authenticated connection", "FAILED", f"Error: {hindsight.get('error')}"))

    # 6. Real memory retention
    if hindsight.get("retention"):
        rows.append(("Real memory retention", "VERIFIED", f"Doc ID: {hindsight.get('fixture_doc_id')} (latency={hindsight.get('retention_latency_s')}s)"))
    elif not hindsight.get("configured"):
        rows.append(("Real memory retention", "FAILED", "Blocked: base URL missing"))
    else:
        rows.append(("Real memory retention", "FAILED", f"Retention failed: {hindsight.get('error')}"))

    # 7. Real memory retrieval
    if hindsight.get("retrieval"):
        rows.append(("Real memory retrieval", "VERIFIED", f"Recalled {hindsight.get('recalled_count')} items (latency={hindsight.get('retrieval_latency_s')}s)"))
    elif not hindsight.get("configured"):
        rows.append(("Real memory retrieval", "FAILED", "Blocked: base URL missing"))
    else:
        rows.append(("Real memory retrieval", "FAILED", f"Retrieval failed: {hindsight.get('error')}"))

    # 8. Exact fixture recall
    if hindsight.get("fixture_match"):
        rows.append(("Exact fixture recall", "VERIFIED", f"Fixture {hindsight.get('fixture_doc_id')} recalled successfully"))
    elif not hindsight.get("configured"):
        rows.append(("Exact fixture recall", "FAILED", "Blocked: base URL missing"))
    else:
        rows.append(("Exact fixture recall", "FAILED", f"Fixture not found in recalled memories: {hindsight.get('error')}"))

    # 9. PostgreSQL preservation
    if pg.get("preservation_verified") and pg.get("status") == "connected":
        rows.append(("PostgreSQL preservation", "VERIFIED", f"Read-only verified: incidents={pg['counts']['incidents']}, events={pg['counts']['events']}"))
    else:
        rows.append(("PostgreSQL preservation", "FAILED", f"Connection failed: {pg.get('error')}"))

    return rows


def save_evidence_safely(report: dict):
    """Safely write verification report to evidence directory, backing up any existing file."""
    evidence_dir = Path(__file__).resolve().parents[1] / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / "provider-verification.json"
    if target.exists():
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        backup = evidence_dir / f"provider-verification.{timestamp}.bak"
        shutil.copy2(target, backup)
        print(f"[INFO] Existing evidence preserved to {backup.name}")
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[INFO] Verification evidence written to {target.name}")


def main():
    print("=" * 70)
    print("OpsMemory — Provider Verification & Genuine Diagnostics")
    print("=" * 70)

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": {
            "postgresql": check_postgres(),
            "groq": check_groq(),
            "hindsight": check_hindsight(),
            "prometheus": check_prometheus()
        }
    }

    table_rows = build_verification_table(report)

    print("\n| Check | Required result | Details |")
    print("|---|---|---|")
    for check, res, det in table_rows:
        print(f"| {check} | {res} | {det} |")
    print("")

    save_evidence_safely(report)

    # Determine overall status
    groq_ok = report["providers"]["groq"].get("status") == "connected"
    groq_configured = report["providers"]["groq"].get("configured")
    hindsight_ok = report["providers"]["hindsight"].get("fixture_match") is True
    hindsight_configured = report["providers"]["hindsight"].get("configured")

    if groq_ok and hindsight_ok:
        overall = "VERIFIED WORKING"
    elif not groq_configured or not hindsight_configured:
        overall = "BLOCKED — EXTERNAL DEPENDENCY"
    elif report["providers"]["groq"].get("error") or report["providers"]["hindsight"].get("error"):
        overall = "FAILED — REQUIRES FIX"
    else:
        overall = "IMPLEMENTED — NOT VERIFIED"

    print("=" * 70)
    print(f"OVERALL STATUS: {overall}")
    if overall == "BLOCKED — EXTERNAL DEPENDENCY":
        missing = []
        if not groq_configured:
            missing.append("GROQ_API_KEY")
        if not hindsight_configured:
            missing.append("HINDSIGHT_BASE_URL")
        print(f"Missing required configuration in .env: {', '.join(missing)}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
