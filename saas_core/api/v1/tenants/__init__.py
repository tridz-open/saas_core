"""Tenant lifecycle API ops: tenants.list/get/create/request_delete/suspend_status/open_login_link."""
from __future__ import annotations

import builtins
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

import frappe

from saas_core.api.v1._common import authenticated, ApiError, client_ip, endpoint, idempotent, paginate, rate_limit, require_login
from saas_core.api.v1.serializers import to_job, to_tenant

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$")
_REASONS = ("dunning", "manual", "abuse", "canceled")
_SECRETISH = re.compile(
    r"(frappe\.?cloud|\bfc[_-]|fcctl|traceback|\bfile \"|/home/|/opt/|/var/|\bat 0x|https?://|"
    r"\btoken\b|\bsecret\b|\bbench\b|\bserver[-_ ]?id\b|[a-z0-9-]+\.(?:frappe\.cloud|internal|local)\b)",
    re.I,
)
GENERIC_ERROR = "The operation could not be completed. Our team has been notified."


def safe_error(msg):
    """Customer-safe error string: never echo anything FC-internal."""
    if not msg:
        return None
    first = str(msg).strip().splitlines()[0] if str(msg).strip() else ""
    if not first or _SECRETISH.search(str(msg)):
        return GENERIC_ERROR
    return first[:300]


def safe_job(doc) -> dict:
    """to_job() plus error / step-message scrubbing."""
    out = dict(to_job(doc))
    out["error"] = safe_error(out.get("error"))
    if out.get("steps"):
        steps = []
        for s in out["steps"]:
            s = dict(s)
            if s.get("message"):
                s["message"] = safe_error(s["message"])
            steps.append(s)
        out["steps"] = steps
    return out


def not_found(what="Resource"):
    return ApiError("not_found", f"{what} not found", 404)


def access(tenant, user, ptype="read"):
    """Owner/admin access; anything else looks like a 404 (no enumeration)."""
    if not tenant or not isinstance(tenant, str):
        raise ApiError("validation_error", "tenant is required", 400, {"tenant": "required"})
    from saas_core import permissions
    try:
        return permissions.assert_tenant_access(tenant, user, ptype)
    except (frappe.DoesNotExistError, frappe.PermissionError):
        raise not_found("Tenant")


def set_status(code):
    if getattr(frappe.local, "response", None) is None:
        frappe.local.response = frappe._dict()
    frappe.local.response["http_status_code"] = code


def _safe_idem_header():
    getter = getattr(frappe, "get_request_header", None)
    try:
        return getter("Idempotency-Key") if getter else None
    except Exception:  # no bound request (bench execute / tests)
        return None


def idem_key():
    return _safe_idem_header() or frappe.form_dict.get("idempotency_key")


def run_idempotent(scope, user, payload, fn, status=None):
    def wrapped():
        res = fn()
        return res
    key = idem_key()
    if key:
        h = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        res = idempotent(key, h, wrapped, scope)
    else:
        res = wrapped()
    if status:
        set_status(status)
    return res


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


RESERVED = frozenset({
    "www", "api", "app", "admin", "mail", "cp", "console", "dashboard", "support", "help", "status", "blog", "docs",
    "billing", "login", "signup", "auth", "static", "assets", "cdn", "ftp", "smtp", "imap", "root", "test", "demo",
    "staging", "dev",
})
SLUG_MSG = {
    "invalid": "Use 4\u201332 lowercase letters, numbers or dashes, starting with a letter.",
    "reserved": "That address is reserved.",
    "taken": "That address is taken.",
    "available": "Available",
}


def _slug_taken(slug) -> bool:
    """Tenant exists, or an unconsumed Checkout Session still reserves the slug."""
    if frappe.db.exists("Tenant", slug):
        return True
    return bool(frappe.db.exists("Checkout Session", {"tenant": slug, "consumed_at": None}))


def _slug_reason(slug):
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return "invalid"
    if slug in RESERVED:
        return "reserved"
    if _slug_taken(slug):
        return "taken"
    return None


def _slugify(raw) -> str:
    s = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", str(raw or "").lower())).strip("-")
    if s and s[0].isdigit():
        s = "x" + s
    return s[:32].rstrip("-")


def _suggest_slug(base):
    if not base:
        return None
    if SLUG_RE.match(base) and _slug_reason(base) is None:
        return base
    for n in range(2, 10):
        cand = f"{base[:32 - len(str(n)) - 1].rstrip('-')}-{n}"
        if SLUG_RE.match(cand) and _slug_reason(cand) is None:
            return cand
    return None


def _check_slug(slug):
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ApiError("validation_error", "Invalid slug", 422, {"slug": "3-32 chars: lowercase letters, digits, hyphens; start with a letter"})
    if slug in RESERVED:
        raise ApiError("validation_error", "That address is reserved", 422, {"slug": "reserved"})
    if frappe.db.exists("Tenant", slug):
        raise ApiError("conflict", "That subdomain is already taken", 409, {"slug": "taken"})


def _tenant_paid(slug) -> bool:
    """True only when an active/trialing Subscription is linked to THIS tenant (set by the checkout flow)."""
    return bool(frappe.db.exists("Subscription", {"tenant": slug, "status": ["in", ["active", "trialing"]]}))


def _new_tenant(user, slug, plan, template, region, recipe_fields, created_via="api"):
    doc = frappe.get_doc({
        "doctype": "Tenant", "name": slug, "slug": slug, "owner_user": user, "status": "pending",
        "plan": plan, "template": template, "region": region, "created_via": created_via,
        "recipe_fields": json.dumps(recipe_fields) if recipe_fields else None,
    })
    doc.insert(ignore_permissions=True)
    return doc


def _entitle(user, plan):
    from saas_core.billing import entitlements
    ok, reason = entitlements.can_create_tenant(user, plan)
    if not ok:
        raise ApiError("forbidden", reason or "Plan limit reached", 403)


def _pending_job(tenant, kind, args=None):
    """Job record that is NOT dispatched (waiting for checkout); billing processor dedupes onto it."""
    doc = frappe.get_doc({"doctype": "Provision Job", "tenant": tenant, "kind": kind, "state": "pending",
                          "attempts": 0, "needs_attention": 0, "dedupe_key": f"{kind}:{tenant}",
                          "args": json.dumps(args or {})})
    doc.insert(ignore_permissions=True)
    return doc


@authenticated()
def list(cursor=None, limit=None, status=None):
    user = require_login()
    rate_limit(f"tenants.list:{user}", 120, 60)
    filters = {"owner_user": user}
    if status:
        filters["status"] = status
    page = paginate("Tenant", filters, ["name"], cursor, limit, order_by="creation desc")
    items = [to_tenant(frappe.get_doc("Tenant", r["name"])) for r in page["items"]]
    return {"items": items, "next_cursor": page["next_cursor"]}


@authenticated()
def get(tenant=None):
    user = require_login()
    rate_limit(f"tenants.get:{user}", 240, 60)
    return to_tenant(access(tenant, user))


@authenticated()
def slug_availability(slug=None):
    """Is `<slug>.<site_domain>` free? Returns {slug, available, reason, message, suggestion}."""
    user = require_login()
    rate_limit(f"tenants.slug_availability:{user}", 60, 60)
    norm = str(slug or "").strip().lower()
    reason = _slug_reason(norm)
    base = _slugify(norm) if reason == "invalid" else norm
    suggestion = None if reason is None else _suggest_slug(base)
    return {"slug": norm, "available": reason is None, "reason": reason,
            "message": SLUG_MSG[reason or "available"], "suggestion": suggestion}


@authenticated(methods=["POST"])
def create(slug=None, plan=None, template=None, region=None, recipe_fields=None):
    user = require_login()
    rate_limit(f"tenants.create:{user}", 10, 60)
    if isinstance(recipe_fields, str):
        recipe_fields = frappe.parse_json(recipe_fields)
    payload = {"slug": slug, "plan": plan, "template": template, "region": region, "recipe_fields": recipe_fields}

    def do():
        _check_slug(slug)
        if not plan or not frappe.db.exists("Plan", plan):
            raise ApiError("validation_error", "Unknown plan", 422, {"plan": "unknown plan"})
        tpl = template or frappe.db.get_value("Plan", plan, "template")
        if tpl and not frappe.db.exists("Template", tpl):
            raise ApiError("validation_error", "Unknown template", 422, {"template": "unknown template"})
        from saas_core.api.v1._selection import check_selectable, is_operator
        check_selectable(user, plan, tpl)
        _entitle(user, plan)
        t = _new_tenant(user, slug, plan, tpl, region, recipe_fields)
        # Provision immediately only for operators (explicit override) or a tenant-linked live subscription.
        needs_checkout = not (is_operator(user) or _tenant_paid(slug))
        if needs_checkout:
            job = _pending_job(slug, "provision")
        else:
            from saas_core import jobs
            job = frappe.get_doc("Provision Job", jobs.enqueue_job(slug, "provision", dedupe_key=f"provision:{slug}"))
        from saas_core import audit
        audit.log("tenant.create", actor=user, target_doctype="Tenant", target=slug, details={"plan": plan})
        out = {"tenant": to_tenant(t), "job": safe_job(job)}
        if needs_checkout:
            out["checkout_required"] = True
        return out

    return run_idempotent("tenants.create", user, payload, do, 202)


@authenticated(methods=["POST"])
def request_delete(tenant=None, confirm_slug=None, reason=None):
    user = require_login()
    rate_limit(f"tenants.request_delete:{user}", 5, 60)
    doc = access(tenant, user, "write")

    def do():
        if confirm_slug != doc.slug:
            raise ApiError("validation_error", "Confirmation does not match", 400, {"confirm_slug": "does not match tenant slug"})
        if doc.status == "terminating":
            raise ApiError("conflict", "Deletion already requested", 409)
        args = {"reason": reason or ""}
        digest = hashlib.sha256(json.dumps({"op": "terminate", "target": doc.name, "args": args}, sort_keys=True,
                                         separators=(",", ":"), default=str).encode()).hexdigest()  # == ports.compute_digest
        apr = frappe.get_doc({"doctype": "Approval Request", "op": "terminate", "target": doc.name,
                              "args": json.dumps(args), "digest": digest, "requested_by": user,
                              "approved_by": "[]", "status": "pending"})
        apr.insert(ignore_permissions=True)
        from saas_core import jobs, audit
        job = frappe.get_doc("Provision Job", jobs.enqueue_job(
            doc.name, "terminate", args={**args, "approval_args": args}, approval=apr.name, dedupe_key=f"terminate:{doc.name}"))
        audit.log("tenant.delete_requested", actor=user, target_doctype="Tenant", target=doc.name, details={"approval": apr.name})
        return safe_job(job)

    return run_idempotent("tenants.request_delete", user, {"t": tenant, "c": confirm_slug, "r": reason}, do, 202)


@authenticated()
def suspend_status(tenant=None):
    user = require_login()
    rate_limit(f"tenants.suspend_status:{user}", 120, 60)
    doc = access(tenant, user)
    suspended = doc.status == "suspended"
    reason = doc.get("suspend_reason") if suspended else None
    if reason not in _REASONS:
        reason = "manual" if suspended else None
    sub = None
    rows = frappe.get_all("Subscription", filters={"tenant": doc.name}, fields=["dunning_stage"], order_by="creation desc", limit=1)
    if rows:
        sub = rows[0]
    return {
        "suspended": suspended,
        "reason": reason,
        "dunning_stage": int((sub or {}).get("dunning_stage") or 0),
        "since": _iso(doc.get("modified")) if suspended else None,
        "resume_by_paying": suspended and reason == "dunning",
    }


@authenticated(methods=["POST"])
def open_login_link(tenant=None):
    user = require_login()
    rate_limit(f"tenants.open_login_link:{user}", 20, 60)
    doc = access(tenant, user)
    if doc.status != "active":
        raise ApiError("conflict", "Tenant is not active", 409)
    from saas_core.provisioning import runner
    from saas_core import audit
    try:
        res = runner.get_port().login_link(doc.fc_site)
    except Exception:
        frappe.log_error("login_link failed")
        raise ApiError("gateway_error", "Could not create login link, try again shortly", 502)
    url = res.get("url") if isinstance(res, dict) else getattr(res, "url", res)
    exp = frappe.utils.now_datetime() + timedelta(minutes=5)
    audit.log("tenant.login_link", actor=user, target_doctype="Tenant", target=doc.name)
    return {"url": str(url), "expires_at": _iso(exp)}


def _daily_change(doc, plan, user):
    """Day-based proration for a tenant with a live gateway Subscription and prices for both plans; None = legacy path."""
    subs = frappe.get_all("Subscription", filters={"tenant": doc.name, "status": ("in", ["active", "past_due", "trialing"])},
                          fields=["name"], limit=1)
    if not subs:
        return None
    sub = frappe.get_doc("Subscription", subs[0].get("name"))
    if not sub.get("gateway") or not sub.get("period_end") or sub.get("plan") != doc.plan:
        return None
    from saas_core.billing import plan_change
    try:
        if plan_change.preview(sub, plan)["direction"] == "same":
            return None  # equal list price: nothing to prorate, keep the legacy flow
    except plan_change.PlanChangeError:
        return None
    try:
        res = plan_change.apply(sub, plan, user)
    except plan_change.PlanChangeError as e:
        raise ApiError("conflict", e.message, 409)
    except Exception:
        frappe.log_error("plan change failed")
        raise ApiError("gateway_error", "Could not change the plan, try again shortly", 502)
    extra = {"proration": res.get("proration"), "payment_required": bool(res.get("payment_required")),
             "payment_url": res.get("payment_url"), "scheduled": bool(res.get("scheduled"))}
    if res.get("payment_required"):
        return extra
    out = safe_job(frappe.get_doc("Provision Job", res["job"])) if res.get("job") else {}
    out.update(extra)
    return out


@authenticated(methods=["POST"])
def change_plan(tenant=None, plan=None):
    user = require_login()
    rate_limit(f"tenants.change_plan:{user}", 10, 60)
    doc = access(tenant, user, "write")

    def do():
        if not plan or not frappe.db.exists("Plan", plan):
            raise ApiError("validation_error", "Unknown plan", 422, {"plan": "unknown plan"})
        if doc.status != "active":
            raise ApiError("conflict", "Tenant not in a changeable state", 409)
        if doc.plan == plan:
            raise ApiError("conflict", "Already on this plan", 409)
        from saas_core.billing import entitlements
        ok, reason = entitlements.can_change_plan(user, doc.name, plan)
        if not ok:
            raise ApiError("forbidden", reason or "Plan change not allowed", 403)
        upgrade = entitlements.plan_change_kind(doc.plan, plan) == "upgrade"
        if entitlements.disk_shrinks(doc.plan, plan):
            raise ApiError("validation_error", "Target plan has a smaller disk; disk shrink is not supported", 422,
                           {"plan": "disk_shrink_not_supported"})
        daily = _daily_change(doc, plan, user)
        if daily is not None:
            return daily
        args = {"plan": plan, "from": doc.plan}
        scheduled = False
        if not upgrade:
            subs = frappe.get_all("Subscription", filters={"tenant": doc.name, "status": ("in", ["active", "trialing", "past_due"])},
                                  fields=["period_end"], limit=1)
            pe = subs[0].get("period_end") if subs else None
            if pe:
                args["effective_at"] = pe.isoformat() if hasattr(pe, "isoformat") else str(pe)
                scheduled = True
        from saas_core import jobs, audit
        job = frappe.get_doc("Provision Job", jobs.enqueue_job(
            doc.name, "change_plan", args=args, dedupe_key=f"change_plan:{doc.name}:{plan}"))
        audit.log("tenant.change_plan_requested", actor=user, target_doctype="Tenant", target=doc.name,
                  details={"plan": plan, "scheduled": scheduled})
        out = safe_job(job)
        if scheduled:
            out["scheduled"] = True
        return out

    return run_idempotent("tenants.change_plan", user, {"t": tenant, "p": plan}, do, 202)


# ---- tenant -> CP callback: the tenant-side setup app reports "setup complete" -------------------------
SETUP_SIG_HEADER = "X-Saas-Signature"
SETUP_MAX_AGE = timedelta(days=7)       # a background retry may arrive late; replays only repeat an idempotent write
_BAD_SIGNATURE = ("invalid_signature", "Invalid signature", 401)


def _signature_header(req):
    """X-Saas-Signature, else any alias header named in site_config saas_core_setup_signature_header_aliases
    (comma list; for tenant-side setup apps predating the generic name). Aliases are off by default."""
    if req is None:
        return None
    h = req.headers.get(SETUP_SIG_HEADER)
    if h:
        return h
    from saas_core.config import get_setting
    for alias in str(get_setting("setup_signature_header_aliases", "") or "").split(","):
        if alias.strip() and req.headers.get(alias.strip()):
            return req.headers.get(alias.strip())
    return None


def _setup_signature_ok(raw: bytes, header: str | None) -> tuple[str | None, bool]:
    """(tenant name, verified). Unknown site, bad body, missing secret and bad MAC are indistinguishable to the caller."""
    tenant, secret = None, None
    try:
        body = json.loads(raw)
        site = body.get("site") if isinstance(body, dict) else None
        if isinstance(site, str) and site:
            tenant = frappe.db.get_value("Tenant", {"fc_site": site}, "name")
        if tenant:
            secret = frappe.get_doc("Tenant", tenant).get_password("setup_secret", raise_exception=False)
    except Exception:  # noqa: BLE001 - malformed JSON etc: treated as a bad signature
        tenant, secret = None, None
    expected = "sha256=" + hmac.new((secret or "-").encode(), raw, hashlib.sha256).hexdigest()  # constant work
    ok = bool(tenant and secret and header and hmac.compare_digest(expected, str(header)))
    return (tenant if ok else None), ok


def _fresh(completed_at) -> bool:
    try:
        t = datetime.fromisoformat(str(completed_at))
    except ValueError:
        return False
    now = datetime.now(timezone.utc) if t.tzinfo else datetime.now()
    return now - SETUP_MAX_AGE <= t <= now + timedelta(days=1)


@endpoint(allow_guest=True, methods=["POST"])
def setup_complete():
    """Guest, but only callable with an HMAC (SHA-256, per-tenant `setup_secret`) over the raw body
    `{"site", "completed_at"}`, sent in header X-Saas-Signature (optional aliases via saas_core_setup_signature_header_aliases) as `sha256=<hex>`. Idempotent: marks
    Tenant.setup_completed_at once and enqueues the welcome email (a no-op once sent)."""
    rate_limit(f"tenants.setup_complete:{client_ip()}", 60, 60)
    req = getattr(frappe, "request", None)
    raw = req.get_data() if req is not None else b""
    header = _signature_header(req)
    name, ok = _setup_signature_ok(raw, header)
    if not ok:
        raise ApiError(*_BAD_SIGNATURE)
    body = json.loads(raw)
    if not _fresh(body.get("completed_at")):
        raise ApiError(*_BAD_SIGNATURE)
    first = not frappe.db.get_value("Tenant", name, "setup_completed_at")
    if first:
        frappe.db.set_value("Tenant", name, "setup_completed_at", frappe.utils.now_datetime())
        from saas_core import audit
        audit.log("tenant.setup_completed", target_doctype="Tenant", target=name)
    if not frappe.db.get_value("Tenant", name, "welcome_sent_at"):
        frappe.enqueue("saas_core.notifications.welcome.send_tenant_welcome", tenant=name, enqueue_after_commit=True)
    return {"tenant": name, "status": "completed" if first else "already_completed"}
