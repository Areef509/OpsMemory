"""Check Groq API from inside the Docker container.
Runs via docker compose exec — uses the container's network namespace.
Never prints key values."""
from __future__ import annotations
import subprocess, sys, json

# Python code to run inside the container
# It reads from env, never prints the key
container_script = """
import os, json, urllib.request, urllib.error, time

api_key = os.environ.get('GROQ_API_KEY', '')
model = os.environ.get('GROQ_MODEL', 'NOT SET')
if not api_key:
    print(json.dumps({'status': 'MISSING_KEY', 'model': model}))
    exit(1)

print(json.dumps({'status': 'KEY_FOUND', 'model': model}))

# Try models list
req = urllib.request.Request(
    'https://api.groq.com/openai/v1/models',
    headers={'Authorization': 'Bearer ' + api_key}
)
t0 = time.monotonic()
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        models = sorted(m['id'] for m in data.get('data', []))
        latency = round(time.monotonic() - t0, 3)
        print(json.dumps({'status': 'OK', 'http': 200, 'latency_s': latency, 'models': models}))
except urllib.error.HTTPError as e:
    latency = round(time.monotonic() - t0, 3)
    try:
        body = e.read(512).decode('utf-8', errors='replace')
        err = json.loads(body).get('error', {}).get('message', '')[:200] if body.strip().startswith('{') else body[:100]
    except Exception:
        err = ''
    print(json.dumps({'status': 'HTTP_ERROR', 'http': e.code, 'latency_s': latency, 'error': err}))
except Exception as ex:
    print(json.dumps({'status': 'ERROR', 'error': str(ex)[:200]}))
"""

cmd = ["docker", "compose", "exec", "backend", "python", "-c", container_script]
print("Running Groq model check from inside Docker container...")
result = subprocess.run(cmd, capture_output=True, text=True, cwd=r"E:\opsmemory")

if result.returncode != 0:
    print(f"docker exec failed: {result.stderr[:300]}")
    sys.exit(1)

for line in result.stdout.strip().splitlines():
    try:
        data = json.loads(line)
        print(json.dumps(data, indent=2))
    except Exception:
        print(line)

