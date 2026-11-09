"""Check which environment variables are present inside the backend Docker container.
Never prints values — only SET or MISSING for each variable."""
from __future__ import annotations
import subprocess, sys

VARS_TO_CHECK = [
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_BASE_URL",
    "HINDSIGHT_EXPERIMENT_BANK_ID",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "OPSMEMORY_API_KEYS",
]

py_lines = ["import os"]
for v in VARS_TO_CHECK:
    py_lines.append(f"print('{v}: ' + ('SET' if os.environ.get('{v}','').strip() else 'MISSING'))")
py_script = "; ".join(py_lines)

cmd = ["docker", "compose", "exec", "backend", "python", "-c", py_script]
print("Checking container env vars (SET/MISSING only)...")
result = subprocess.run(cmd, capture_output=True, text=True, cwd=r"E:\opsmemory")
if result.returncode != 0:
    print(f"Error: {result.stderr[:300]}")
    sys.exit(1)
print(result.stdout)
