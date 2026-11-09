"""Check available Groq models. Never prints API key or masked key. Safe to run."""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from dotenv import load_dotenv
load_dotenv()

api_key = os.environ.get("GROQ_API_KEY", "")
if not api_key:
    print("GROQ_API_KEY: MISSING")
    sys.exit(1)
print("GROQ_API_KEY: SET")

req = urllib.request.Request(
    "https://api.groq.com/openai/v1/models",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
)
t0 = time.monotonic()
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        latency = round(time.monotonic() - t0, 3)
        models = sorted(m["id"] for m in data.get("data", []))
        print(f"HTTP 200 — {len(models)} models available (latency {latency}s):")
        for m in models:
            print(f"  {m}")
        # Save evidence (no secrets)
        evidence = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "connected",
            "http_status": 200,
            "latency_s": latency,
            "models_available": models,
        }
        out = Path(__file__).resolve().parents[1] / "evidence" / "groq-models.json"
        out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"\nEvidence saved to evidence/groq-models.json")
except urllib.error.HTTPError as e:
    latency = round(time.monotonic() - t0, 3)
    # Read error body but only print error code (not full response)
    try:
        body = e.read(512).decode("utf-8", errors="replace")
        err_msg = json.loads(body).get("error", {}).get("message", "")[:200] if body else ""
    except Exception:
        err_msg = ""
    print(f"HTTP {e.code} — {err_msg}")
    # Save evidence
    evidence = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "error",
        "http_status": e.code,
        "latency_s": latency,
        "error_message": err_msg,
    }
    out = Path(__file__).resolve().parents[1] / "evidence" / "groq-models.json"
    out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"Evidence saved to evidence/groq-models.json")
    sys.exit(1)
except Exception as ex:
    print(f"Error: {type(ex).__name__}: {str(ex)[:200]}")
    sys.exit(1)

