"""OpsMemory API. Runtime is fail-closed: PostgreSQL, Groq and Hindsight are not mocked."""
import json, os, secrets, time
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field
from .core.recovery import PrometheusRecoveryVerifier, RecoveryMetricsError
from .core.remediation import Adapter, AuthorizationError
from .engine import Engine
from .integrations.groq_client import GroqClient, LLMError, mask_secret
from .integrations.hindsight_memory import HindsightMemory, HindsightError
from .store_memory import PostgresStore

app = FastAPI(title="OpsMemory Incident Response Agent", version="1.0.0")
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))
_engine = None
_store = None

class LazyGroq:
    def __init__(self): self._client = None
    def _get(self):
        if self._client is None: self._client = GroqClient()
        return self._client
    def chat_json(self, system, user):
        return self._get().chat_json(system, user)
    def check_connection(self):
        return self._get().check_connection()

class LazyHindsight:
    def __init__(self): self._client = None
    def _get(self):
        if self._client is None: self._client = HindsightMemory()
        return self._client
    def recall_similar(self, query): return self._get().recall_similar(query)
    def retain_post_mortem(self, *args, **kwargs): return self._get().retain_post_mortem(*args, **kwargs)
    def retain_experiment_seed(self, *args, **kwargs): return self._get().retain_experiment_seed(*args, **kwargs)
    def check_connection(self): return self._get().check_connection()


def _users():
    out = {}
    for item in filter(None, os.environ.get("OPSMEMORY_API_KEYS", "").split(",")):
        parts = item.split(":", 2)
        if len(parts) != 3 or not all(parts): raise RuntimeError("OPSMEMORY_API_KEYS entries must be key:user:role")
        key, user, role = parts
        if role not in {"viewer", "sre", "admin"}: raise RuntimeError(f"unsupported role: {role}")
        out[key] = (user, role)
    return out


def auth(x_api_key: str = Header(default="")):
    for key, identity in _users().items():
        if secrets.compare_digest(key, x_api_key): return identity
    raise HTTPException(401, "invalid API key")


def require_operator(user=Depends(auth)):
    if user[1] not in {"sre", "admin"}: raise HTTPException(403, "operator role required")
    return user


def require_admin(user=Depends(auth)):
    if user[1] != "admin": raise HTTPException(403, "admin role required")
    return user


def engine():
    global _engine, _store
    if _engine is None:
        _store = PostgresStore()
        adapters = {}
        # Controlled safe adapters for demonstration and remediation
        if os.environ.get("OPSMEMORY_ENABLE_DEMO_ADAPTER", "true").lower() == "true":
            adapters["demo-noop"] = Adapter("demo-noop", lambda: {"note": "no-op demonstration action", "executed_at": time.time()}, True)
            adapters["restart-service"] = Adapter("restart-service", lambda: {"note": "safe service process restart executed", "executed_at": time.time()}, True)
            adapters["scale-pool"] = Adapter("scale-pool", lambda: {"note": "database connection pool capacity scaled by 50%", "executed_at": time.time()}, True)
            adapters["clear-cache"] = Adapter("clear-cache", lambda: {"note": "stale connection and query cache cleared", "executed_at": time.time()}, True)
        try:
            dependencies = json.loads(os.environ.get("OPS_DEPENDENCIES_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("OPS_DEPENDENCIES_JSON must be valid JSON") from exc
        critical = {x.strip() for x in os.environ.get("OPS_CRITICAL_SERVICES", "").split(",") if x.strip()}
        _engine = Engine(_store, LazyGroq(), LazyHindsight(), adapters,
                         dependencies=dependencies, critical_services=critical)
    return _engine


class Batch(BaseModel):
    events: list[dict] = Field(min_length=1, max_length=500)
class Decision(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=1000)
class RemReq(BaseModel):
    recommendation_index: int = Field(ge=0, le=100)
    adapter: str = Field(min_length=1, max_length=64)

@app.get("/", include_in_schema=False)
def dashboard():
    page = FRONTEND_DIR / "index.html"
    if not page.is_file(): raise HTTPException(404, "dashboard assets are not installed")
    return FileResponse(page)

@app.get("/health")
def health(): return {"status": "alive"}

@app.get("/ready")
def ready():
    try:
        store = PostgresStore()
        return {"status": "database_ready", "postgres": "connected", "groq_configuration": "present" if os.environ.get("GROQ_API_KEY") else "missing",
                "hindsight_configuration": "present" if os.environ.get("HINDSIGHT_BASE_URL") else "missing",
                "note": "Provider configuration is reported, not live-connectivity verified."}
    except Exception as exc:
        raise HTTPException(503, f"not ready: {type(exc).__name__}")

@app.get("/diagnostics/providers")
def diagnostics_providers(u=Depends(auth)):
    """Safe diagnostics for PostgreSQL, Groq, Hindsight, and Prometheus without exposing secrets."""
    db_status = "connected"
    db_err = None
    try:
        store = PostgresStore()
    except Exception as exc:
        db_status = "error"
        db_err = f"{type(exc).__name__}: {exc}"

    groq_api_key = os.environ.get("GROQ_API_KEY")
    groq_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    if groq_api_key and groq_api_key.strip():
        try:
            groq_diag = GroqClient(api_key=groq_api_key, model=groq_model).check_connection()
        except Exception as exc:
            groq_diag = {"configured": True, "connected": False, "model": groq_model, "error": f"{type(exc).__name__}: {exc}"}
    else:
        groq_diag = {"configured": False, "connected": False, "model": groq_model, "note": "GROQ_API_KEY is not set in .env"}

    h_base = os.environ.get("HINDSIGHT_BASE_URL")
    h_bank = os.environ.get("HINDSIGHT_BANK_ID", "opsmemory")
    if h_base and h_base.strip():
        try:
            h_diag = HindsightMemory(base_url=h_base, bank_id=h_bank).check_connection()
        except Exception as exc:
            h_diag = {"configured": True, "connected": False, "bank_id": h_bank, "error": f"{type(exc).__name__}: {exc}"}
    else:
        h_diag = {"configured": False, "connected": False, "bank_id": h_bank, "note": "HINDSIGHT_BASE_URL is not set in .env"}

    prom_configured = bool(os.environ.get("PROMETHEUS_BASE_URL") and os.environ.get("RECOVERY_ERROR_RATE_QUERY"))

    return {
        "user": {"name": u[0], "role": u[1]},
        "database": {"status": db_status, "error": db_err},
        "groq": groq_diag,
        "hindsight": h_diag,
        "prometheus": {"configured": prom_configured}
    }

@app.get("/adapters")
def list_adapters(u=Depends(auth), e: Engine = Depends(engine)):
    return [{"name": name, "simulated": ad.simulated} for name, ad in e.adapters.items()]

@app.get("/audit")
def list_audit(u=Depends(require_operator), e: Engine = Depends(engine)):
    if hasattr(e.store, "audit_log_entries"):
        return e.store.audit_log_entries(limit=100)
    return getattr(e.store, "audit", [])

@app.post("/events")
def post_events(b: Batch, u=Depends(require_operator), e: Engine = Depends(engine)):
    synthetic_mode = os.environ.get("OPSMEMORY_INGEST_SYNTHETIC", "false").lower() == "true"
    trusted_batch = [{**event, "synthetic": synthetic_mode} for event in b.events]
    ids, rejected = e.ingest(trusted_batch); return {"incident_ids": ids, "rejected": rejected, "ingestion_mode": "synthetic" if synthetic_mode else "external"}

@app.get("/incidents")
def incidents(u=Depends(auth), e: Engine = Depends(engine)):
    return e.store.list_incidents() if hasattr(e.store, "list_incidents") else list(e.store.incidents.values())

@app.get("/incidents/{iid}")
def get_incident(iid: str, u=Depends(auth), e: Engine = Depends(engine)):
    try:
        inc = e._get_incident(iid)
        inc["approval_requests"] = e.get_incident_approvals(iid)
        return inc
    except KeyError: raise HTTPException(404, "incident not found")

@app.post("/incidents/{iid}/investigate")
def investigate(iid: str, use_memory: bool = True, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return e.investigate(iid, use_memory)
    except KeyError: raise HTTPException(404, "incident not found")
    except LLMError as exc: raise HTTPException(502, f"LLM provider error: {exc}")

@app.post("/incidents/{iid}/remediations")
def request_rem(iid: str, r: RemReq, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(e.request_remediation(iid, r.recommendation_index, r.adapter))
    except KeyError: raise HTTPException(404, "incident not found")
    except ValueError as exc: raise HTTPException(400, str(exc))

@app.post("/approvals/{rid}/decision")
def decision(rid: str, d: Decision, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(e.decide(rid, u[0], u[1], d.approve, d.reason))
    except KeyError: raise HTTPException(404, "approval not found")
    except AuthorizationError as exc: raise HTTPException(403, str(exc))
    except ValueError as exc: raise HTTPException(409, str(exc))

@app.post("/approvals/{rid}/execute")
def run(rid: str, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(e.execute(rid, u[0]))
    except KeyError: raise HTTPException(404, "approval not found")
    except ValueError as exc: raise HTTPException(409, str(exc))

@app.post("/incidents/{iid}/verify")
def verify(iid: str, u=Depends(require_admin), e: Engine = Depends(engine)):
    try:
        samples = PrometheusRecoveryVerifier().collect()
        pm, retained = e.verify_and_close(iid, samples, synthetic=False)
        return {"postmortem": pm, "retained_to_hindsight": retained is not None, "evidence_source": "prometheus"}
    except KeyError: raise HTTPException(404, "incident not found")
    except RecoveryMetricsError as exc: raise HTTPException(503, str(exc))
