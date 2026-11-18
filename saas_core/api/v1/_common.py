"""Shared helpers for the v1 API: errors, pagination, idempotency, rate limiting, auth guards."""
from __future__ import annotations

import base64
import functools
import hashlib
import json
from typing import Any, Callable

import frappe

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
IDEMPOTENCY_TTL = 24 * 3600


class ApiError(Exception):
    """Raise from any endpoint; `endpoint` (or the site exception handler) renders the Error schema."""

    def __init__(self, code: str, message: str, http_status: int = 400,
                 field_errors: dict | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.http_status_code = http_status  # honoured by frappe's own handler
        self.field_errors = field_errors or None
        self.retry_after = retry_after


def require_experimental(feature: str) -> None:
    """403 `experimental_disabled` unless site_config saas_core_experimental enables `feature` (see docs/STATUS.md)."""
    from saas_core.experimental import ExperimentalDisabled, require
    try:
        require(feature)
    except ExperimentalDisabled as e:
        raise ApiError("experimental_disabled", str(e), 403)


def _response():
    r = getattr(frappe.local, "response", None)
    if r is None:
        r = frappe._dict()
        frappe.local.response = r
    return r


def set_status(code: int) -> None:
    _response()["http_status_code"] = code


def error_response(exc: ApiError) -> dict:
    """Set the HTTP status and return the `Error` schema body."""
    set_status(exc.http_status)
    err: dict = {"code": exc.code, "message": exc.message}
    if exc.field_errors:
        err["field_errors"] = dict(exc.field_errors)
    rid = getattr(frappe.local, "request_id", None)
    if rid:
        err["request_id"] = str(rid)
    if exc.retry_after is not None:
        err["retry_after"] = int(exc.retry_after)
    return {"error": err}


def not_found(what: str = "Resource") -> ApiError:
    return ApiError("not_found", f"{what} not found", 404)


def guest_guard(fn: Callable) -> Callable:
    """Reject Guest with frappe.PermissionError BEFORE any endpoint logic (raises; never swallowed)."""
    @functools.wraps(fn)
    def guarded(*a, **k):
        if getattr(frappe.session, "user", None) in (None, "", "Guest"):
            raise frappe.PermissionError("Authentication required")
        return fn(*a, **k)
    guarded.__saas_guest_guard__ = True
    return guarded


def authenticated(**whitelist_kw) -> Callable:
    """frappe.whitelist (never allow_guest) + guest_guard. Use for every customer endpoint."""
    whitelist_kw.pop("allow_guest", None)
    explicit = whitelist_kw.pop("methods", None)
    def deco(fn: Callable) -> Callable:
        verbs = list(explicit) if explicit else (["GET"] if _endpoint_id(fn) in READ_ENDPOINTS else ["POST"])
        guarded = guest_guard(fn)
        guarded.__saas_methods__ = verbs
        return frappe.whitelist(methods=verbs, **whitelist_kw)(guarded)
    return deco


# Explicit allowlist of read-only endpoints ("<module under saas_core.api.v1>.<function>"), served over GET.
# Everything else decorated with `endpoint()` is POST only (Frappe enforces CSRF on POST, not GET).
READ_ENDPOINTS = frozenset({
    "auth.config", "auth.me",
    "plans.list", "templates.list",
    "api_tokens.list",
    "invoices.list", "invoices.get", "invoices.pdf",
    "subscription.get", "billing.preview_cancel", "billing.preview_change",
    "admin.tenants.list", "admin.beta_access.list",
    "tenants.list", "tenants.get", "tenants.suspend_status", "tenants.slug_availability",    
    "tenants.team.list", "tenants.backups.list",
    "jobs.get", "jobs.list",
    "support.tickets.get", "support.tickets.list",
    "events.stream",
})


def _endpoint_id(fn: Callable) -> str:
    mod = getattr(fn, "__module__", "") or ""
    prefix = "saas_core.api.v1."
    if mod.startswith(prefix):
        mod = mod[len(prefix):]
    return f"{mod}.{fn.__name__}"


def endpoint(allow_guest: bool = False, methods: list[str] | tuple[str, ...] | None = None) -> Callable:
    """whitelist + translate exceptions into the Error schema (non-guest endpoints are guest-guarded).

    `methods` defaults to ["GET"] for ids in READ_ENDPOINTS and ["POST"] otherwise, so a state-changing
    endpoint can never be triggered by a cross-site GET (login CSRF, admin ops CSRF)."""
    def deco(fn: Callable) -> Callable:
        verbs = list(methods) if methods else (["GET"] if _endpoint_id(fn) in READ_ENDPOINTS else ["POST"])
        if not allow_guest:
            fn = guest_guard(fn)
        @functools.wraps(fn)
        def wrapper(*a, **k):
            if not allow_guest and getattr(frappe.session, "user", None) in (None, "", "Guest"):
                raise frappe.PermissionError("Authentication required")
            try:
                return fn(*a, **k)
            except ApiError as e:
                return error_response(e)
            except frappe.DoesNotExistError:
                return error_response(not_found())
            except frappe.AuthenticationError:
                return error_response(ApiError("unauthorized", "Authentication required", 401))
            except frappe.PermissionError:
                return error_response(ApiError("forbidden", "Not allowed", 403))
            except frappe.ValidationError as e:
                return error_response(ApiError("validation_error", str(e) or "Invalid request", 422))
        wrapper.__saas_guest_guard__ = not allow_guest
        wrapper.__saas_methods__ = verbs
        return frappe.whitelist(allow_guest=allow_guest, methods=verbs)(wrapper)
    return deco


# ---- auth ---------------------------------------------------------------
def current_user() -> str:
    return getattr(frappe.session, "user", None) or "Guest"


def require_login() -> str:
    user = current_user()
    if user in ("Guest", ""):
        raise ApiError("unauthorized", "Authentication required", 401)
    return user


def client_ip() -> str:
    return str(getattr(frappe.local, "request_ip", None) or "unknown")


# ---- request parsing -----------------------------------------------------
def json_body(params: dict | None = None) -> dict:
    """Request params/body as a plain dict: form_dict overlaid with explicit non-None kwargs
    (pass `locals()` first thing in an endpoint)."""
    d = dict(getattr(frappe, "form_dict", None) or {})
    d.pop("cmd", None)
    d.update({k: v for k, v in (params or {}).items() if v is not None})
    return d


def require_fields(body: dict, *names: str) -> None:
    missing = {n: "required" for n in names if body.get(n) in (None, "")}
    if missing:
        raise ApiError("bad_request", "Missing required fields: " + ", ".join(missing), 400, missing)


def as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            r = json.loads(v)
            return r if isinstance(r, dict) else {}
        except ValueError:
            return {}
    return {}


def as_list(v: Any) -> list:
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str) and v.strip():
        try:
            r = json.loads(v)
            return r if isinstance(r, list) else []
        except ValueError:
            return []
    return []


def request_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---- pagination ----------------------------------------------------------
def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(int(offset)).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        off = int(base64.urlsafe_b64decode(str(cursor).encode()).decode())
    except Exception:
        raise ApiError("bad_request", "Invalid cursor", 400, {"cursor": "invalid"})
    if off < 0:
        raise ApiError("bad_request", "Invalid cursor", 400, {"cursor": "invalid"})
    return off


def clamp_limit(limit: Any) -> int:
    try:
        n = int(limit) if limit not in (None, "") else DEFAULT_LIMIT
    except (TypeError, ValueError):
        raise ApiError("bad_request", "Invalid limit", 400, {"limit": "invalid"})
    if n < 1 or n > MAX_LIMIT:
        raise ApiError("bad_request", f"limit must be 1..{MAX_LIMIT}", 400, {"limit": "out of range"})
    return n


def paginate(doctype: str, filters: Any, fields: Any, cursor: str | None = None, limit: Any = None,
             order_by: str = "creation desc", serializer: Callable | None = None) -> dict:
    off, n = decode_cursor(cursor), clamp_limit(limit)
    rows = frappe.get_all(doctype, filters=filters, fields=fields, order_by=order_by, limit=off + n + 1)
    rows = list(rows)[off:]
    has_more = len(rows) > n
    rows = rows[:n]
    items = [serializer(r) for r in rows] if serializer else rows
    return {"items": items, "next_cursor": encode_cursor(off + n) if has_more else None}


# ---- idempotency -----------------------------------------------------------
def idempotency_key() -> str | None:
    key = None
    getter = getattr(frappe, "get_request_header", None)
    if getter:
        try:
            key = getter("Idempotency-Key")
        except Exception:
            key = None
    return key or (getattr(frappe, "form_dict", None) or {}).get("idempotency_key") or None


def idempotent(key: str | None, req_hash: str, fn: Callable[[], Any], scope: str = "") -> Any:
    """Run fn once per (user, key); replay the stored result; different payload => 409."""
    if not key:
        return fn()
    ck = f"saas_core:idem:{scope}:{current_user()}:{key}"
    cache = frappe.cache()
    hit = cache.get_value(ck)
    if hit:
        if hit.get("hash") != req_hash:
            raise ApiError("idempotency_mismatch", "Idempotency-Key reused with a different request", 409)
        return hit.get("result")
    result = fn()
    cache.set_value(ck, {"hash": req_hash, "result": result}, expires_in_sec=IDEMPOTENCY_TTL)
    return result


# ---- rate limiting ---------------------------------------------------------
def _site() -> str:
    return str(getattr(getattr(frappe, "local", None), "site", None) or "")


def rate_limit(key: str, limit: int, window: int) -> None:
    """Fixed window counter: atomic raw-redis INCR, EXPIRE set when the window starts (never extended)."""
    ck = f"{_site()}:saas_core:rl:{key}"
    cache = frappe.cache()
    if hasattr(cache, "connection_pool") and hasattr(cache, "incr"):
        n = int(cache.incr(ck))
        if n == 1 or cache.ttl(ck) in (-1, None):
            cache.expire(ck, int(window))
    else:  # test stub without raw redis
        n = int(cache.get_value(ck) or 0) + 1
        cache.set_value(ck, n, expires_in_sec=window)
    if n > limit:
        raise ApiError("rate_limited", "Too many requests", 429, retry_after=window)


# ---- shared lookups (sibling modules imported lazily) -----------------------
def tenant_access(tenant: str, user: str | None = None, ptype: str = "read"):
    """Ownership gate: any non-owner/unknown tenant => 404 (no enumeration)."""
    from saas_core.permissions import assert_tenant_access
    try:
        return assert_tenant_access(tenant, user or current_user(), ptype)
    except (frappe.DoesNotExistError, frappe.PermissionError):
        raise not_found("Tenant")


def latest_subscription(tenant: str | None = None, user: str | None = None):
    filters = {"tenant": tenant} if tenant else {"owner_user": user or current_user()}
    rows = frappe.get_all("Subscription", filters=filters, fields=["name", "status", "creation"],
                          order_by="creation desc")
    rows = sorted(rows, key=lambda r: r.get("status") == "canceled")  # prefer live subs, stable
    return frappe.get_doc("Subscription", rows[0]["name"]) if rows else None


def gateway_for(name: str | None):
    from saas_core.gateways.registry import get_gateway
    return get_gateway(name)


def utc_in(seconds: int) -> str:
    import datetime as dt
    return (dt.datetime.utcnow() + dt.timedelta(seconds=seconds)).isoformat()


def iso_or_none(v):
    from .serializers import iso
    return iso(v)
