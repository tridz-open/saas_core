"""Shared fakes + OpenAPI schema checks for E2 API tests (fakes injected via sys.modules)."""
import base64, importlib, json, pathlib, sys, types, datetime as dt
import yaml, jsonschema

SPEC = yaml.safe_load((pathlib.Path(__file__).parent.parent / "openapi" / "v1.yaml").read_text())


def check_schema(name, data):
    jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}).validate(json.loads(json.dumps(data, default=str)))


class FakePort:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.backups = [
            {"name": f"bk{i}", "created_at": dt.datetime(2026, 1, i + 1), "size_bytes": 100 * i, "kind": "scheduled", "parts": ["database", "public"]}
            for i in range(5)]

    def login_link(self, site):
        self.calls.append(("login_link", site))
        if self.fail: raise RuntimeError("fc-internal boom frappecloud token=abc")
        return f"https://{site}/sso?t=1"

    def list_backups(self, site):
        self.calls.append(("list_backups", site)); return list(self.backups)

    def backup_download_link(self, site, backup):
        self.calls.append(("dl", site, backup)); return f"https://dl.test/{backup}"


_UNDO = []


def restore():
    """Undo package-attribute patches (sys.modules is evicted by conftest)."""
    while _UNDO:
        pkg, child, old = _UNDO.pop()
        if old is None: pkg.__dict__.pop(child, None)
        else: setattr(pkg, child, old)


def _mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        _UNDO.append((sys.modules[parent], child, sys.modules[parent].__dict__.get(child)))
        setattr(sys.modules[parent], child, m)  # `from pkg import child` prefers the attribute
    return m


def install(frappe, user="alice@x.com", roles=("SaaS Customer",)):
    """Install fake collaborators; returns namespace with port/jobs/audit/entitle handles."""
    frappe.session.user = user
    frappe._roles = list(roles)
    ns = types.SimpleNamespace(port=FakePort(), audit=[], enqueued=[], allow=(True, None))

    class ApiError(Exception):
        def __init__(self, code, message, http_status=400, field_errors=None):
            super().__init__(message); self.code, self.message, self.http_status, self.field_errors = code, message, http_status, field_errors

    def require_login():
        if frappe.session.user == "Guest": raise ApiError("unauthorized", "login", 401)
        return frappe.session.user

    def idempotent(key, request_hash, fn, scope=""):
        if not key: return fn()
        k = f"idem:{scope}:{frappe.session.user}:{key}"; hit = frappe.cache().get_value(k)
        if hit:
            if hit["h"] != request_hash: raise ApiError("idempotency_mismatch", "mismatch", 409)
            return hit["r"]
        r = fn(); frappe.cache().set_value(k, {"h": request_hash, "r": r}); return r

    def paginate(doctype, filters, fields, cursor=None, limit=None, order_by="creation desc"):
        off = int(base64.b64decode(cursor).decode()) if cursor else 0
        n = int(limit or 20)
        rows = frappe.get_all(doctype, filters=filters, fields=fields, order_by=order_by)
        nxt = base64.b64encode(str(off + n).encode()).decode() if off + n < len(rows) else None
        return {"items": rows[off: off + n], "next_cursor": nxt}

    def require_experimental(feature):
        from saas_core.experimental import ExperimentalDisabled, require
        try: require(feature)
        except ExperimentalDisabled as e: raise ApiError("experimental_disabled", str(e), 403)

    ns.rate = []
    _ins = frappe._Doc.insert
    def insert(self, **k):
        self.setdefault("creation", dt.datetime(2026, 1, 1)); return _ins(self, **k)
    frappe._Doc.insert = insert
    import os
    if os.environ.get("E2_REAL"):
        sys.modules.pop("saas_core.api.v1._common", None); sys.modules.pop("saas_core.api.v1.serializers", None)
    else: _mod("saas_core.api.v1._common", ApiError=ApiError, require_login=require_login, authenticated=lambda **k: (lambda fn: fn), idempotent=idempotent,
         paginate=paginate, rate_limit=lambda key, limit, window: ns.rate.append(key),
         client_ip=lambda: "203.0.113.9", require_experimental=require_experimental, endpoint=lambda **k: (lambda fn: fn))
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v

    def to_tenant(d):
        return {"name": d.name, "slug": d.slug, "status": d.status, "plan": d.plan, "template": d.get("template"),
                "region": d.get("region"), "url": None, "custom_domain": d.get("custom_domain"),
                "created_at": iso(d.get("creation") or dt.datetime(2026, 1, 1)), "active_job": d.get("active_job"),
                "suspend_reason": d.get("suspend_reason")}

    def to_job(d):
        return {"name": d.name, "tenant": d.tenant, "kind": d.kind, "state": d.state, "progress": 0.0,
                "steps": [{"seq": s.get("seq", 1), "name": s.get("name", "x"), "state": s.get("state", "pending"), "message": None} for s in (d.get("steps") or [])],
                "attempts": d.get("attempts") or 0, "needs_attention": bool(d.get("needs_attention")), "error": "The operation failed. Our team has been notified." if d.get("error") else None,
                "created_at": iso(d.get("creation") or dt.datetime(2026, 1, 1)), "finished_at": iso(d.get("finished_at"))}

    def to_ticket(d):
        return {"name": d.name, "tenant": d.get("tenant"), "subject": d.subject, "status": d.status, "priority": d.get("priority"),
                "created_at": iso(d.get("creation") or dt.datetime(2026, 1, 1)),
                "replies": [{"from_staff": bool(r.get("from_staff")), "body": r["body"], "at": iso(r.get("at"))} for r in (d.get("replies") or [])]}
    if not os.environ.get("E2_REAL"): _mod("saas_core.api.v1.serializers", to_tenant=to_tenant, to_job=to_job, to_ticket=to_ticket)

    def enqueue_job(tenant, kind, *, args=None, approval=None, dedupe_key=None):
        for (dt_, n), d in list(frappe._store.docs.items()):
            if dt_ == "Provision Job" and dedupe_key and d.get("dedupe_key") == dedupe_key and d.state not in ("succeeded", "failed"):
                return n
        d = frappe.get_doc({"doctype": "Provision Job", "name": f"PJ-{len(ns.enqueued)+1:06d}", "tenant": tenant, "kind": kind,
                            "state": "pending", "attempts": 0, "approval": approval, "dedupe_key": dedupe_key, "args": args})
        d.insert(); ns.enqueued.append((tenant, kind, approval)); return d.name
    _mod("saas_core.jobs", enqueue_job=enqueue_job)

    def log(action, *, actor=None, target_doctype=None, target=None, details=None):
        ns.audit.append((action, actor, target, details)); return "AUD-1"
    _mod("saas_core.audit", log=log)

    def assert_tenant_access(name, user=None, ptype="read"):
        user = user or frappe.session.user
        d = frappe._store.docs.get(("Tenant", name))
        if not d or (d.owner_user != user and "SaaS Admin" not in frappe.get_roles(user)):
            raise frappe.DoesNotExistError(name)
        return d
    _mod("saas_core.permissions", assert_tenant_access=assert_tenant_access)
    _mod("saas_core.billing", __path__=[])
    _mod("saas_core.billing.entitlements", can_create_tenant=lambda u, p: ns.allow)
    _mod("saas_core.provisioning", __path__=[])
    _mod("saas_core.provisioning.runner", get_port=lambda: ns.port)
    for k in [k for k in sys.modules if k.startswith("saas_core.api.v1.") and k not in ("saas_core.api.v1._common", "saas_core.api.v1.serializers")]:
        del sys.modules[k]
    return ns


def seed(frappe, slug="acme", owner="alice@x.com", status="active", plan="starter"):
    if not frappe.db.exists("Plan", plan): frappe.get_doc({"doctype": "Plan", "name": plan, "code": plan, "template": "erp", "is_public": 1, "available": 1, "sort_order": 1}).insert()
    if not frappe.db.exists("Template", "erp"): frappe.get_doc({"doctype": "Template", "name": "erp", "is_public": 1}).insert()
    return frappe.get_doc({"doctype": "Tenant", "name": slug, "slug": slug, "owner_user": owner, "status": status, "plan": plan,
                           "template": "erp", "fc_site": f"{slug}.fc.example"}).insert()


def sub(frappe, user="alice@x.com", status="active", tenant="acme"):
    return frappe.get_doc({"doctype": "Subscription", "owner_user": user, "status": status, "name": f"SUB-{tenant}", "tenant": tenant}).insert()


def mod(name):
    return importlib.import_module(name)
