from contextvars import ContextVar

_current_tenant = ContextVar("opsmemory_tenant", default="default")

def set_tenant(tenant_id: str):
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    _current_tenant.set(tenant_id.strip())
    return _current_tenant.get()

def current_tenant() -> str:
    return _current_tenant.get()


def reset_tenant(token) -> None:
    _current_tenant.reset(token)
