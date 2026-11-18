import importlib, yaml, pathlib


def test_every_spec_op_has_callable(frappe):
    d = yaml.safe_load((pathlib.Path(__file__).parent.parent / "openapi/v1.yaml").read_text())
    missing = []
    for p in d["paths"]:
        if "webhooks" in p or p.endswith("events.stream"):
            continue
        mod, _, fn = p.removeprefix("/api/method/").rpartition(".")
        if not hasattr(importlib.import_module(mod), fn):
            missing.append(p)
    assert not missing


import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    import types, sys
    from saas_core.billing import entitlements as real  # real module may be stubbed; patch stub
    stub = sys.modules["saas_core.billing.entitlements"]
    stub.can_change_plan = lambda u, t, p: ns.allow if ns.allow != (True, None) else (True, "")
    stub.plan_change_kind = lambda o, n: "upgrade" if (frappe.db.get_value("Plan", n, "sort_order") or 0) > (frappe.db.get_value("Plan", o, "sort_order") or 0) else "downgrade"
    stub.disk_shrinks = lambda o, n: bool(ns.shrink)
    ns.shrink = False
    ns.t = h.mod("saas_core.api.v1.tenants")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    for n, so in (("starter", 1), ("pro", 2)):
        frappe.get_doc({"doctype": "Plan", "name": n, "code": n, "template": "erp", "sort_order": so}).insert() if not frappe.db.exists("Plan", n) else None
        frappe.db.set_value("Plan", n, "sort_order", so)
    yield ns
    h.restore()


def test_upgrade_immediate_ignores_tenant_cap(env):
    h.seed(env.f)
    ns = env
    ns.allow = (True, None)  # can_create_tenant would be (True,None); a cap failure must not matter
    out = env.t.change_plan(tenant="acme", plan="pro")
    assert env.enqueued == [("acme", "change_plan", None)] and "scheduled" not in out
    assert env.f.local.response["http_status_code"] == 202


def test_disk_shrink_422(env):
    h.seed(env.f); env.shrink = True
    with pytest.raises(env.ApiError) as ei:
        env.t.change_plan(tenant="acme", plan="pro")
    assert ei.value.args and env.enqueued == []


def test_downgrade_scheduled_at_period_end(env):
    h.seed(env.f, plan="pro")
    env.f.get_doc({"doctype": "Subscription", "owner_user": "alice@x.com", "status": "active", "name": "SUB-1",
                   "tenant": "acme", "period_end": "2026-02-01 00:00:00"}).insert()
    out = env.t.change_plan(tenant="acme", plan="starter")
    j = env.f.get_doc("Provision Job", out["name"])
    assert out["scheduled"] is True and "effective_at" in str(j.args)
