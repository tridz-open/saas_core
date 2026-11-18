"""Tests for api/v1/_common.py, serializers and openapi op -> callable mapping."""
import importlib
import pathlib
import sys

import pytest
import yaml

SPEC = pathlib.Path(__file__).parent.parent / "openapi" / "v1.yaml"
MINE = ("plans", "templates", "signup", "login", "logout", "me", "password_reset", "verify_email", "checkout",
        "subscription", "invoices", "payment_methods")


def test_error_response_shape(frappe):
    from saas_core.api.v1._common import ApiError, error_response
    out = error_response(ApiError("validation_error", "bad", 422, {"f": "x"}))
    assert out == {"error": {"code": "validation_error", "message": "bad", "field_errors": {"f": "x"},
                             "request_id": "req-1"}}
    assert frappe.local.response["http_status_code"] == 422


def test_endpoint_translates_errors(frappe):
    from saas_core.api.v1._common import ApiError, endpoint

    @endpoint()
    def a():
        raise ApiError("conflict", "c", 409)

    @endpoint()
    def b():
        raise frappe.DoesNotExistError("x")
    assert a()["error"]["code"] == "conflict" and frappe.local.response["http_status_code"] == 409
    assert b()["error"]["code"] == "not_found" and frappe.local.response["http_status_code"] == 404


def test_require_login(frappe):
    from saas_core.api.v1._common import ApiError, require_login
    frappe.session.user = "Guest"
    with pytest.raises(ApiError) as e:
        require_login()
    assert e.value.http_status == 401
    frappe.session.user = "a@x.io"
    assert require_login() == "a@x.io"


def test_paginate(frappe):
    from saas_core.api.v1._common import ApiError, paginate
    for i in range(5):
        frappe.get_doc({"doctype": "Thing", "name": f"t{i}", "creation": i}).insert()
    p1 = paginate("Thing", None, ["name"], None, 2)
    assert [r["name"] for r in p1["items"]] == ["t4", "t3"] and p1["next_cursor"]
    p2 = paginate("Thing", None, ["name"], p1["next_cursor"], 2)
    assert [r["name"] for r in p2["items"]] == ["t2", "t1"]
    p3 = paginate("Thing", None, ["name"], p2["next_cursor"], 2)
    assert [r["name"] for r in p3["items"]] == ["t0"] and p3["next_cursor"] is None
    with pytest.raises(ApiError):
        paginate("Thing", None, ["name"], "!!notbase64", 2)
    with pytest.raises(ApiError):
        paginate("Thing", None, ["name"], None, 1000)


def test_idempotent_replay_and_mismatch(frappe):
    from saas_core.api.v1._common import ApiError, idempotent
    calls = []
    f = lambda: calls.append(1) or {"ok": len(calls)}
    assert idempotent("k1", "h1", f) == {"ok": 1}
    assert idempotent("k1", "h1", f) == {"ok": 1} and len(calls) == 1
    with pytest.raises(ApiError) as e:
        idempotent("k1", "OTHER", f)
    assert e.value.http_status == 409 and e.value.code == "idempotency_mismatch"
    assert idempotent(None, "h", f) == {"ok": 2}


def test_rate_limit(frappe):
    from saas_core.api.v1._common import ApiError, rate_limit
    rate_limit("k", 2, 60); rate_limit("k", 2, 60)
    with pytest.raises(ApiError) as e:
        rate_limit("k", 2, 60)
    assert e.value.http_status == 429 and e.value.retry_after == 60


def test_serializers_match_schemas(frappe):
    from saas_core.api.v1 import serializers as s
    t = s.to_tenant(frappe._dict(name="acme", slug="acme", status="active", plan="pro", fc_site="acme.x.io"))
    assert t["url"] == "https://acme.x.io" and t["status"] == "active"
    j = s.to_job(frappe._dict(name="PJ-1", tenant="acme", kind="provision", state="running", error="fc: secret trace",
                              steps=[{"seq": 1, "name": "a", "state": "succeeded", "message": "fc internals"},
                                     {"seq": 2, "name": "b", "state": "running"}]))
    assert j["progress"] == 0.5 and "secret" not in str(j) and "internals" not in str(j)
    inv = s.to_invoice(frappe._dict(name="INV-1", status="paid", total_amount=500, currency="USD", pdf_url="/f.pdf"))
    assert inv["total"] == {"amount": 500, "currency": "USD"} and inv["has_pdf"] is True
    assert s.to_me("a@x.io", csrf_token="c")["csrf_token"] == "c"
    assert s.to_backup({"name": "b", "created_at": "2024-01-01"})["kind"] == "on_demand"
    assert s.to_ticket(frappe._dict(name="T", subject="s", replies=[{"body": "hi", "from_staff": 1}]))["replies"][0]["from_staff"]
    assert s.to_plan(frappe._dict(code="p", title="P", limits='{"a":1}', features='["x"]'))["limits"] == {"a": 1}
    assert s.to_template(frappe._dict(slug="t", title="T"))["signup_schema"] == {}
    assert "tenant" in s.to_subscription(frappe._dict(tenant="acme", plan="p", status="active"))


def _ops():
    spec = yaml.safe_load(SPEC.read_text())
    for path in spec["paths"]:
        if path.startswith("/api/method/saas_core.api.v1."):
            yield path.rsplit("/api/method/saas_core.api.v1.", 1)[1]


@pytest.mark.parametrize("op", [o for o in _ops() if o.split(".")[0] in MINE])
def test_openapi_op_has_callable(frappe, op):
    parts = op.split(".")
    if len(parts) == 1:
        mod, fn = "auth", parts[0]
    else:
        mod, fn = ".".join(parts[:-1]), parts[-1]
    m = importlib.import_module(f"saas_core.api.v1.{mod}")
    assert callable(getattr(m, fn))


# ---- T5 review regressions (P1-6, P2-5) ----
def _captured_whitelist(frappe):
    calls = []
    def wl(*a, **k):
        if a and callable(a[0]):
            return a[0]
        def deco(fn):
            calls.append((fn.__name__, k))
            return fn
        return deco
    frappe.whitelist = wl
    return calls


def test_endpoint_methods_default_post_and_read_allowlist(frappe):
    calls = _captured_whitelist(frappe)
    from saas_core.api.v1._common import endpoint
    @endpoint()
    def create_thing(): return 1
    @endpoint(methods=["GET"])
    def other(): return 1
    assert calls[0][1]["methods"] == ["POST"]
    assert calls[1][1]["methods"] == ["GET"]


def test_real_endpoints_verbs(frappe):
    calls = _captured_whitelist(frappe)
    import importlib
    for m in ("auth", "checkout", "checkout_verify", "plans", "invoices", "admin.ops", "admin.tenants"):
        importlib.import_module(f"saas_core.api.v1.{m}")
    verbs = {n: k["methods"] for n, k in calls}
    for n in ("login", "signup", "logout", "run", "create_session", "verify_payment"):
        assert verbs[n] == ["POST"], n
    for n in ("config", "me", "pdf"):
        assert verbs[n] == ["GET"], n


def test_rate_limit_atomic_raw_incr(frappe):
    from saas_core.api.v1 import _common
    class Raw:
        connection_pool = object()
        def __init__(s): s.d, s.exp = {}, {}
        def incr(s, k): s.d[k] = s.d.get(k, 0) + 1; return s.d[k]
        def ttl(s, k): return s.exp.get(k, -1)
        def expire(s, k, w): s.exp[k] = w
    r = Raw()
    frappe.cache = lambda: r
    _common.rate_limit("k", 2, 60); _common.rate_limit("k", 2, 60)
    assert list(r.exp.values()) == [60]
    with pytest.raises(_common.ApiError):
        _common.rate_limit("k", 2, 60)
    assert r.d and all(":saas_core:rl:k" in k for k in r.d)


def test_all_mutating_endpoints_reject_get(frappe):
    """Every registered endpoint (endpoint() and authenticated()) is POST-only unless it is a known read."""
    import importlib
    import pkgutil
    calls = _captured_whitelist(frappe)
    import saas_core.api.v1 as pkg
    from saas_core.api.v1._common import READ_ENDPOINTS
    mods = []
    for mi in pkgutil.walk_packages(pkg.__path__, "saas_core.api.v1."):
        if not mi.name.rsplit(".", 1)[-1].startswith("_"):
            mods.append(importlib.import_module(mi.name))
    seen = {}
    for m in mods:
        for k, v in vars(m).items():
            if callable(v) and getattr(v, "__saas_methods__", None) and v.__module__ == m.__name__:
                eid = v.__module__[len("saas_core.api.v1."):] + "." + v.__name__
                seen[eid] = v.__saas_methods__
    assert len(seen) >= 40, sorted(seen)
    for eid, verbs in seen.items():
        if eid in READ_ENDPOINTS:
            assert verbs == ["GET"], eid
        else:
            assert verbs == ["POST"], f"{eid} accepts {verbs}"
    for eid in ("tenants.list", "tenants.get", "tenants.suspend_status", "tenants.team.list",
                "tenants.backups.list", "jobs.get", "jobs.list", "support.tickets.get",
                "support.tickets.list", "events.stream"):
        assert seen.get(eid) == ["GET"], eid
    for eid in ("tenants.create", "tenants.team.invite", "tenants.backups.restore"):
        if eid in seen:
            assert seen[eid] == ["POST"], eid


def test_to_job_started_at_and_step_aliases(frappe):
    from saas_core.api.v1 import serializers as s
    j = s.to_job(frappe._dict(name="PJ-1", tenant="acme", kind="provision", state="running", creation="2026-11-18 10:00:00",
                              steps=[{"seq": 2, "name": "b", "state": "running", "started_at": "2026-11-18 10:02:00"},
                                     {"seq": 1, "name": "a", "state": "succeeded", "started_at": "2026-11-18 10:01:00"}]))
    assert j["started_at"] == "2026-11-18 10:01:00"  # earliest step start
    assert [(x["key"], x["status"]) for x in j["steps"]] == [("b", "running"), ("a", "succeeded")]
    assert all(x["key"] == x["name"] and x["status"] == x["state"] for x in j["steps"])
    assert s.to_job(frappe._dict(name="PJ-2", tenant="t", kind="provision", state="pending", creation="2026-11-18 10:00:00"))["started_at"] == "2026-11-18 10:00:00"
    assert s.to_job(frappe._dict(name="PJ-3", tenant="t", kind="provision", state="pending", started_at="2026-11-18 09:00:00"))["started_at"] == "2026-11-18 09:00:00"
    assert s.to_job(frappe._dict(name="PJ-4", tenant="t", kind="provision", state="pending"))["started_at"] is None
