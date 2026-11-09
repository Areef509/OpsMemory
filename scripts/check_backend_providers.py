"""Groq connectivity test via backend HTTP API (avoids local IP block).
Tests the backend's /diagnostics/providers endpoint, which is authorized.
Never exposes secrets. Records redacted evidence."""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

BACKEND_URL = os.environ.get("OPSMEMORY_TEST_BACKEND_URL", "http://localhost:8000")

# Load admin key from env
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from dotenv import load_dotenv
load_dotenv()

admin_key = os.environ.get("OPSMEMORY_API_KEYS", "")
# OPSMEMORY_API_KEYS format: "key:name:role,key2:name2:role2"
# Find an admin or operator key
api_key_to_use = None
if admin_key:
    for entry in admin_key.split(","):
        parts = entry.strip().split(":")
        if len(parts) == 3 and parts[2] in ("admin", "operator"):
            api_key_to_use = parts[0]
            break

print(f"Backend URL: {BACKEND_URL}")
print(f"Admin key found: {'YES' if api_key_to_use else 'NO — will try unauthenticated'}")

# 1. Test /health
req = urllib.request.Request(f"{BACKEND_URL}/health")
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
        print(f"/health: {resp.status} — {body}")
except Exception as e:
    print(f"/health FAILED: {e}")
    sys.exit(1)

# 2. Test /ready (shows Groq/Hindsight config status)
req = urllib.request.Request(f"{BACKEND_URL}/ready")
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
        print(f"/ready: {resp.status} — {json.dumps(body)}")
except Exception as e:
    print(f"/ready FAILED: {e}")

# 3. Test /diagnostics/providers (requires auth)
if api_key_to_use:
    req = urllib.request.Request(
        f"{BACKEND_URL}/diagnostics/providers",
        headers={"X-API-Key": api_key_to_use}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            # Redact any secrets from body before printing
            if "api_key" in str(body).lower():
                print("/diagnostics/providers: Response received (redacting potential key fields)")
            else:
                print(f"/diagnostics/providers: {resp.status} — {json.dumps(body, indent=2)[:800]}")
    except urllib.error.HTTPError as e:
        err_body = e.read(256).decode("utf-8", errors="replace")
        print(f"/diagnostics/providers: HTTP {e.code} — {err_body[:200]}")
    except Exception as ex:
        print(f"/diagnostics/providers FAILED: {type(ex).__name__}: {str(ex)[:200]}")
else:
    print("/diagnostics/providers: SKIPPED — no admin/operator key in OPSMEMORY_API_KEYS")
    print("NOTE: Set OPSMEMORY_API_KEYS=yourkey:name:admin in .env to enable this check")

