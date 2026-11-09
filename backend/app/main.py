"""OpsMemory API. Runtime is fail-closed: PostgreSQL, Groq and Hindsight are not mocked."""
import json, os, secrets, time, threading
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest
from pathlib import Path
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from .core.recovery import RecoveryMetricsError, collect_for_services
from .core.remediation import Adapter, AuthorizationError
from .core.adapter_registry import AdapterRegistry, AdapterSpec
from .engine import Engine, VerificationBlocked
from .ratelimit import RateLimiter
from .integrations.groq_client import GroqClient, LLMError, mask_secret
from .integrations.hindsight_memory import HindsightMemory, HindsightError
from .store_memory import PostgresStore

@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        try:
            if _engine is not None:
                memory = getattr(_engine, "memory", None)
                close = getattr(memory, "aclose", None)
                if close is not None:
                    await close()
        finally:
            if _store is not None:
                _store.close()


app = FastAPI(
    title="OpsMemory Incident Response Agent",
    version="1.0.0",
    lifespan=lifespan,
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        if request.url.path != "/metrics": response.headers["Cache-Control"] = "no-store"
        return response

_rl = RateLimiter(int(os.environ.get("OPSMEMORY_RATE_LIMIT_PER_MIN", "240")))
_auth_fail = RateLimiter(int(os.environ.get("OPSMEMORY_AUTH_FAIL_LIMIT_PER_MIN", "10")))


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else "unknown"
        if request.url.path != "/metrics" and (_auth_fail.blocked(ip) or not _rl.allow(ip)):
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        return await call_next(request)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

HTTP_REQUESTS = Counter(
    "opsmemory_http_requests_total",
    "HTTP requests served by the OpsMemory API",
    ["method", "status"],
)
HTTP_ROUTE_REQUESTS = Counter(
    "opsmemory_http_route_requests_total",
    "HTTP requests grouped by normalized FastAPI route template",
    ["method", "route", "status"],
)
HTTP_ERRORS = Counter(
    "opsmemory_http_errors_total",
    "HTTP responses with 4xx or 5xx status, grouped by normalized route",
    ["method", "route", "status"],
)
HTTP_IN_PROGRESS = Gauge(
    "opsmemory_http_requests_in_progress",
    "HTTP requests currently being handled by the OpsMemory API",
)
HTTP_REQUEST_DURATION = Histogram(
    "opsmemory_http_request_duration_seconds",
    "OpsMemory API HTTP request duration in seconds",
    ["method", "route"],
)
POOL_UP = Gauge("opsmemory_postgres_pool_up", "Whether the initialized PostgreSQL pool is available")
POOL_CONNECTIONS = Gauge("opsmemory_postgres_pool_connections", "Current PostgreSQL pool connections")
POOL_AVAILABLE = Gauge("opsmemory_postgres_pool_available_connections", "Available PostgreSQL pool connections")
POOL_WAITING = Gauge("opsmemory_postgres_pool_waiting_requests", "Requests currently waiting for a PostgreSQL connection")
POOL_REQUESTS = Gauge("opsmemory_postgres_pool_requests_total", "Cumulative requests served by the PostgreSQL pool")
POOL_REQUEST_ERRORS = Gauge("opsmemory_postgres_pool_request_errors_total", "Cumulative PostgreSQL pool request errors")
POOL_CONNECTION_ERRORS = Gauge("opsmemory_postgres_pool_connection_errors_total", "Cumulative PostgreSQL pool connection errors")
POOL_MIN = Gauge("opsmemory_postgres_pool_min_connections", "Configured minimum PostgreSQL pool size")
POOL_MAX = Gauge("opsmemory_postgres_pool_max_connections", "Configured maximum PostgreSQL pool size")

WORKFLOW_OPERATIONS = Counter(
    "opsmemory_workflow_operations_total",
    "OpsMemory workflow operations grouped by operation and outcome",
    ["operation", "outcome"],
)
WORKFLOW_DURATION = Histogram(
    "opsmemory_workflow_duration_seconds",
    "OpsMemory workflow operation duration in seconds",
    ["operation"],
)

def _workflow_call(operation, function, *args, **kwargs):
    started = time.perf_counter()
    outcome = "success"
    try:
        return function(*args, **kwargs)
    except Exception:
        outcome = "error"
        raise
    finally:
        WORKFLOW_OPERATIONS.labels(operation, outcome).inc()
        WORKFLOW_DURATION.labels(operation).observe(time.perf_counter() - started)


@app.middleware("http")
async def tenant_context_middleware(request, call_next):
    from .tenant_context import _current_tenant
    token = _current_tenant.set("default")
    try:
        supplied = request.headers.get("x-api-key", "")
        for key, identity in _users().items():
            if secrets.compare_digest(key.encode("utf-8"), supplied.encode("utf-8", "ignore")):
                _current_tenant.set(identity[2])
                break
        return await call_next(request)
    finally:
        _current_tenant.reset(token)


@app.middleware("http")
async def collect_http_metrics(request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    started = time.perf_counter()
    status = "500"
    HTTP_IN_PROGRESS.inc()
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        route_obj = request.scope.get("route")
        route = getattr(route_obj, "path", "unmatched") or "unmatched"
        elapsed = time.perf_counter() - started
        HTTP_IN_PROGRESS.dec()
        HTTP_REQUESTS.labels(request.method, status).inc()
        HTTP_ROUTE_REQUESTS.labels(request.method, route, status).inc()
        HTTP_REQUEST_DURATION.labels(request.method, route).observe(elapsed)
        if int(status) >= 400:
            HTTP_ERRORS.labels(request.method, route, status).inc()

def _refresh_pool_metrics():
    # Never create the application store during a Prometheus scrape.
    if _store is None:
        POOL_UP.set(0)
        return
    try:
        stats = _store.pool_stats()
        POOL_UP.set(1)
        POOL_CONNECTIONS.set(stats.get("pool_size", 0))
        POOL_AVAILABLE.set(stats.get("pool_available", 0))
        POOL_WAITING.set(stats.get("requests_waiting", 0))
        POOL_REQUESTS.set(stats.get("requests_num", 0))
        POOL_REQUEST_ERRORS.set(stats.get("requests_errors", 0))
        POOL_CONNECTION_ERRORS.set(stats.get("connections_errors", 0))
        POOL_MIN.set(stats.get("pool_min", 0))
        POOL_MAX.set(stats.get("pool_max", 0))
    except Exception:
        POOL_UP.set(0)

@app.get("/metrics", include_in_schema=False)
def metrics():
    _refresh_pool_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))
_engine = None
_store = None
_store_lock = threading.Lock()


def get_store():
    """Return the process-wide PostgreSQL repository and its shared pool."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PostgresStore()
    return _store

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
    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _users():
    out = {}
    for item in filter(None, os.environ.get("OPSMEMORY_API_KEYS", "").split(",")):
        parts = item.split(":")
        if len(parts) not in {3, 4} or not all(parts):
            raise RuntimeError("OPSMEMORY_API_KEYS entries must be key:user:role[:tenant]")
        key, user, role = parts[:3]
        tenant = parts[3] if len(parts) == 4 else "default"
        if role not in {"viewer", "sre", "admin"}:
            raise RuntimeError(f"unsupported role: {role}")
        if tenant in {".", ".."} or any(ch in tenant for ch in "/\\\\"):
            raise RuntimeError("invalid tenant identifier")
        out[key] = (user, role, tenant)
    return out


def auth(request: Request, x_api_key: str = Header(default="")):
    from .tenant_context import set_tenant
    supplied, match = x_api_key.encode("utf-8", "ignore"), None  # bytes: compare_digest raises TypeError on non-ASCII str
    for key, identity in _users().items():
        if secrets.compare_digest(key.encode("utf-8"), supplied): match = identity
    if match:
        set_tenant(match[2])
        return match
    _auth_fail.hit(request.client.host if request.client else "unknown")
    raise HTTPException(401, "invalid API key")


def require_operator(user=Depends(auth)):
    if user[1] not in {"sre", "admin"}: raise HTTPException(403, "operator role required")
    return user


def require_admin(user=Depends(auth)):
    if user[1] != "admin": raise HTTPException(403, "admin role required")
    return user


def _run_canary_remediation():
    """Execute one authenticated operation against the fixed internal canary only."""
    import urllib.request

    base = os.environ.get("CANARY_BASE_URL", "").rstrip("/")
    token = os.environ.get("CANARY_REMEDIATION_TOKEN", "")
    if base != "http://canary:8081" or not token:
        raise RuntimeError("canary remediation is not safely configured")

    req = urllib.request.Request(
        base + "/remediate",
        data=b"{}",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("canary remediation returned a non-success status")
        result = json.loads(response.read())

    if (
        result.get("service") != "opsmemory-canary"
        or result.get("operation") != "clear_canary_fault"
        or result.get("current_state") != "healthy"
    ):
        raise RuntimeError("canary remediation response failed validation")

    return {
        "service": "opsmemory-canary",
        "operation": "clear_canary_fault",
        "previous_state": result.get("previous_state"),
        "current_state": "healthy",
        "changed": result.get("changed"),
        "executed_at": result.get("executed_at"),
    }



def _run_gateway_remediation():
    """Restore only the fixed local hackathon payment-gateway test service."""
    import urllib.request

    base = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
    token = os.environ.get("GATEWAY_REMEDIATION_TOKEN", "")
    if base != "http://gateway:8082" or not token:
        raise RuntimeError("gateway remediation is not safely configured")

    req = urllib.request.Request(
        base + "/remediate",
        data=b"{}",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("gateway remediation returned a non-success status")
        result = json.loads(response.read())

    if (
        result.get("service") != "payment-gateway"
        or result.get("operation") != "restore_gateway_test_service"
        or result.get("current_state") != "healthy"
        or not result.get("executed_at")
    ):
        raise RuntimeError("gateway remediation response failed validation")

    return {
        "service": "payment-gateway",
        "operation": "restore_gateway_test_service",
        "previous_state": result.get("previous_state"),
        "current_state": "healthy",
        "changed": result.get("changed"),
        "executed_at": result.get("executed_at"),
        "environment": "hackathon-test",
    }


def engine():
    global _engine, _store
    if _engine is None:
        _store = get_store()
        adapters = {}
        canary_token = os.environ.get("CANARY_REMEDIATION_TOKEN", "")
        canary_base = os.environ.get("CANARY_BASE_URL", "").rstrip("/")
        if canary_token and canary_base:
            if canary_base != "http://canary:8081":
                raise RuntimeError("CANARY_BASE_URL must be the fixed internal canary endpoint")
            adapters["canary-clear-fault"] = Adapter(
                "canary-clear-fault", _run_canary_remediation, simulated=False
            )

        gateway_token = os.environ.get("GATEWAY_REMEDIATION_TOKEN", "")
        gateway_base = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
        if gateway_token and gateway_base:
            if gateway_base != "http://gateway:8082":
                raise RuntimeError("GATEWAY_BASE_URL must be the fixed internal gateway test endpoint")
            adapters["gateway-restore-test"] = Adapter(
                "gateway-restore-test", _run_gateway_remediation, simulated=False
            )
        registry = None
        if os.environ.get("OPSMEMORY_USE_ADAPTER_REGISTRY", "false").lower() == "true":
            # Opt-in: declarative, bounded adapters with service/environment/role/expiry enforcement. Default OFF so the
            # existing verified canary workflow is unchanged until you have confirmed the environment label below.
            registry = AdapterRegistry()
            if "canary-clear-fault" in adapters:
                registry.register(AdapterSpec(
                    id="canary-clear-fault", target_service="opsmemory-canary", permitted_action="clear_canary_fault",
                    required_role="sre", risk_level="low", timeout_s=20.0, idempotent=True,
                    preconditions=("incident_service_matches_target", "incident_not_synthetic", "environment_in_scope", "incident_investigated"),
                    environment_scope=tuple(x.strip() for x in os.environ.get("CANARY_ENVIRONMENT_SCOPE", "canary").split(",") if x.strip()),
                    rollback="Restart the canary with CANARY_START_DEGRADED=true to re-create the fault; no production state is touched.",
                    audit_fields=("incident_id", "request_id", "approver", "executor", "result"),
                    verification="Independent Prometheus samples for opsmemory-canary taken after execution (error rate and p99 within thresholds)."),
                    adapters["canary-clear-fault"].run)
            if "gateway-restore-test" in adapters:
                registry.register(AdapterSpec(
                    id="gateway-restore-test",
                    target_service="payment-gateway",
                    permitted_action="restore_gateway_test_service",
                    required_role="sre",
                    risk_level="medium",
                    timeout_s=20.0,
                    idempotent=True,
                    preconditions=("incident_service_matches_target", "incident_not_synthetic",
                                   "environment_in_scope", "incident_investigated"),
                    environment_scope=("hackathon-test",),
                    rollback="Re-create the degraded state only by restarting the isolated gateway test container with GATEWAY_START_DEGRADED=true.",
                    audit_fields=("incident_id", "request_id", "approver", "executor", "result"),
                    verification="Independent post-execution Prometheus samples from actual payment-gateway test-service requests.",
                ), adapters["gateway-restore-test"].run)
                adapters = registry.adapters()
        try:
            dependencies = json.loads(os.environ.get("OPS_DEPENDENCIES_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("OPS_DEPENDENCIES_JSON must be valid JSON") from exc
        critical = {x.strip() for x in os.environ.get("OPS_CRITICAL_SERVICES", "").split(",") if x.strip()}
        _engine = Engine(_store, LazyGroq(), LazyHindsight(), adapters,
                         dependencies=dependencies, critical_services=critical, registry=registry)
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
        store = get_store()
        return {"status": "database_ready", "postgres": "connected", "groq_configuration": "present" if os.environ.get("GROQ_API_KEY") else "missing",
                "hindsight_configuration": "present" if os.environ.get("HINDSIGHT_BASE_URL") else "missing",
                "note": "Provider configuration is reported, not live-connectivity verified."}
    except Exception as exc:
        raise HTTPException(503, f"not ready: {type(exc).__name__}")

@app.get("/me")
def me(u=Depends(auth)): return {"name": u[0], "role": u[1]}

@app.get("/diagnostics/providers")
async def diagnostics_providers(u=Depends(require_operator)):
    """Safe diagnostics for PostgreSQL, Groq, Hindsight, and Prometheus without exposing secrets."""
    db_status = "connected"
    db_err = None
    try:
        store = get_store()
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
        h_client = HindsightMemory(base_url=h_base, bank_id=h_bank)
        try:
            h_diag = await h_client.acheck_connection()
        except Exception as exc:
            h_diag = {"configured": True, "connected": False, "bank_id": h_bank, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            await h_client.aclose()
    else:
        h_diag = {"configured": False, "connected": False, "bank_id": h_bank, "note": "HINDSIGHT_BASE_URL is not set in .env"}

    prom_configured = bool(os.environ.get("PROMETHEUS_BASE_URL") and os.environ.get("RECOVERY_BINDINGS_JSON", "").strip())

    return {
        "user": {"name": u[0], "role": u[1]},
        "database": {"status": db_status, "error": db_err},
        "groq": groq_diag,
        "hindsight": h_diag,
        "prometheus": {"configured": prom_configured}
    }

@app.get("/adapters")
def list_adapters(u=Depends(auth), e: Engine = Depends(engine)):
    if getattr(e, "registry", None): return e.registry.describe()
    return [{"name": name, "simulated": ad.simulated} for name, ad in e.adapters.items()]

@app.get("/audit")
def list_audit(u=Depends(require_operator), e: Engine = Depends(engine)):
    if hasattr(e.store, "audit_log_entries"):
        return e.store.audit_log_entries(limit=100)
    return getattr(e.store, "audit", [])

@app.post("/events")
def post_events(b: Batch, u=Depends(require_operator), e: Engine = Depends(engine)):
    synthetic_mode = os.environ.get("OPSMEMORY_INGEST_SYNTHETIC", "false").lower() == "true"
    # One-way only: a client may mark events as synthetic fixtures (they are then never retained to Hindsight),
    # but can never mark real events as non-synthetic: the server alone decides "external" mode.
    def _is_fixture(ev): return isinstance(ev.get("metadata"), dict) and ev["metadata"].get("synthetic_fixture") is True
    trusted_batch = [{**event, "synthetic": synthetic_mode or _is_fixture(event)} for event in b.events]
    ids, rejected = _workflow_call("event_ingestion", e.ingest, trusted_batch); return {"incident_ids": ids, "rejected": rejected, "ingestion_mode": "synthetic" if synthetic_mode else "external"}

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
    try: return _workflow_call("investigation", e.investigate, iid, use_memory)
    except KeyError: raise HTTPException(404, "incident not found")
    except LLMError as exc: raise HTTPException(502, f"LLM provider error: {exc}")

@app.post("/incidents/{iid}/compare-memory")
def compare_memory(iid: str, u=Depends(require_operator), e: Engine = Depends(engine)):
    try:
        return _workflow_call("memory_comparison", e.compare_memory, iid, u[0])
    except KeyError:
        raise HTTPException(404, "incident not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except LLMError as exc:
        raise HTTPException(502, f"LLM provider error: {exc}")
    except HindsightError as exc:
        raise HTTPException(503, f"Hindsight provider error: {exc}")

@app.get("/incidents/{iid}/experiments")
def list_incident_experiments(iid: str, u=Depends(require_operator), e: Engine = Depends(engine)):
    try:
        e._get_incident(iid)
        return e.store.list_investigation_experiments(iid)
    except KeyError:
        raise HTTPException(404, "incident not found")

@app.post("/incidents/{iid}/remediations")
def request_rem(iid: str, r: RemReq, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(_workflow_call("remediation_request", e.request_remediation, iid, r.recommendation_index, r.adapter))
    except KeyError: raise HTTPException(404, "incident not found")
    except ValueError as exc: raise HTTPException(400, str(exc))

@app.post("/approvals/{rid}/decision")
def decision(rid: str, d: Decision, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(_workflow_call("approval_decision", e.decide, rid, u[0], u[1], d.approve, d.reason))
    except KeyError: raise HTTPException(404, "approval not found")
    except AuthorizationError as exc: raise HTTPException(403, str(exc))
    except ValueError as exc: raise HTTPException(409, str(exc))

@app.post("/approvals/{rid}/execute")
def run(rid: str, u=Depends(require_operator), e: Engine = Depends(engine)):
    try: return vars(_workflow_call("remediation_execution", e.execute, rid, u[0]))
    except KeyError: raise HTTPException(404, "approval not found")
    except ValueError as exc: raise HTTPException(409, str(exc))



@app.post("/incidents/{iid}/attest-canary-startup-cause")
def attest_canary_startup_cause(iid: str, u=Depends(require_admin), e: Engine = Depends(engine)):
    """Record a narrowly scoped operator-attested configuration finding; never confirm causality."""
    if iid != "6352b63a021d":
        raise HTTPException(404, "incident not found")
    try:
        inc = e._get_incident(iid)
    except KeyError:
        raise HTTPException(404, "incident not found")

    if inc.get("synthetic") is not False or inc.get("services") != ["opsmemory-canary"]:
        raise HTTPException(409, "incident does not match the isolated canary")
    verification = inc.get("verification") or {}
    evidence = verification.get("evidence") or {}
    if inc.get("state") != "resolved" or verification.get("resolved") is not True:
        raise HTTPException(409, "independent recovery verification is required")
    if evidence.get("source") != "prometheus" or "opsmemory-canary" not in evidence.get("services", []):
        raise HTTPException(409, "independent canary Prometheus evidence is required")

    real = [
        a for a in inc.get("approvals", [])
        if a.get("state") == "executed"
        and a.get("simulated") is False
        and a.get("adapter") == "canary-clear-fault"
    ]
    if not real:
        raise HTTPException(409, "real canary remediation record is required")

    event_ids = [
        item.get("event_id") for item in inc.get("timeline", [])
        if item.get("event_id")
    ]
    if not event_ids:
        raise HTTPException(409, "incident evidence event is missing")

    statement = (
        "Operator-attested configuration finding, NOT an independently confirmed root cause: "
        "the canary Compose configuration set CANARY_START_DEGRADED=true, and its startup log "
        "reported initial degraded=True. This explains the deliberately degraded startup state. "
        "It does not establish a production cause or prove an independent causal chain."
    )
    now = time.time()
    inc["causal_evidence"] = {
        "verified": False,
        "evidence_ids": event_ids,
        "evidence_type": "operator_attestation",
        "attested_by": u[0],
        "attested_at_epoch": now,
        "statement": statement,
    }
    pm = inc.setdefault("postmortem", {})
    pm["root_cause"] = {"text": statement, "status": "operator_attested"}
    pm["causal_evidence"] = inc["causal_evidence"]
    inc["postmortem"] = pm
    e._save_incident(inc)

    memory_status = {"status": "not_attempted"}
    try:
        result = e.memory.retain_post_mortem(
            f"{iid}-operator-attestation",
            statement,
            verified=False,
            extra_metadata={
                "service": "opsmemory-canary",
                "environment": "canary",
                "root_cause_status": "operator_attested",
                "evidence_type": "operator_attestation",
                "evidence_event_ids": ",".join(event_ids),
                "outcome": "configuration_finding_not_causal_confirmation",
            },
        )
        memory_status = {
            "status": "retained" if getattr(result, "success", True) else "failed",
            "verified": False,
        }
    except Exception as exc:
        memory_status = {"status": "failed", "error": type(exc).__name__}

    inc["memory_correction"] = memory_status
    e._save_incident(inc)
    if hasattr(e.store, "append_audit"):
        e.store.append_audit(u[0], "canary_root_cause_operator_attestation", {
            "incident_id": iid,
            "root_cause_status": "operator_attested",
            "causal_confirmation": False,
            "memory_status": memory_status,
            "evidence_event_ids": event_ids,
        })
    return {
        "incident_id": iid,
        "root_cause": pm["root_cause"],
        "causal_confirmation": False,
        "memory_correction": memory_status,
    }

@app.post("/incidents/{iid}/verify")
def verify(iid: str, u=Depends(require_admin), e: Engine = Depends(engine)):
    """Fail-closed independent verification: needs a real executed remediation AND a per-service monitoring binding
    (RECOVERY_BINDINGS_JSON) whose series identity matches; samples must post-date the remediation."""
    try:
        pm, retained = _workflow_call("recovery_verification", e.verify_recovery_for_incident, iid, lambda services, since: collect_for_services(services, since))
        inc = e._get_incident(iid)
        return {"postmortem": pm, "retained_to_hindsight": retained is not None, "memory_retention": inc.get("memory_retention"),
                "evidence_source": (inc.get("verification") or {}).get("evidence")}
    except KeyError: raise HTTPException(404, "incident not found")
    except VerificationBlocked as exc: raise HTTPException(409, str(exc))
    except RecoveryMetricsError as exc: raise HTTPException(503, str(exc))
