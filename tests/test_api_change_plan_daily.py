"""tenants.change_plan with day-based proration (real plan_change + proration over the API test fakes)."""
import datetime as dt, importlib.util, pathlib, sys
import pytest
import _api_helpers as h

START, END = dt.datetime(2026, 1, 1), dt.datetime(2026, 2, 1)
ROOT = pathlib.Path(__file__).parent.parent / "saas_core" / "billing"


class FakeGw:
    def __init__(self):
        self.calls = []

    def create_payment_link(self, **kw):
        self.calls.append(("link", kw)); return {"id": "plink_9", "short_url": "https://rzp.io/i/y"}

    def refund_payment(self, pid, amount, *, notes=None, receipt=""):
        self.calls.append(("refund", pid, amount)); return {"id": "rfnd_9", "status": "processed", "amount": amount}

    def update_subscription_plan(self, ref, plan_id, *, at_cycle_end=True):
        self.calls.append(("update", ref, plan_id)); return {}


def _load(name):
    spec = importlib.util.spec_from_file_location(f"saas_core.billing.{name}", ROOT / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    setattr(sys.modules["saas_core.billing"], name, m)
    spec.loader.exec_module(m); return m


@pytest.fixture
def env(frappe, monkeypatch):
    ns = h.install(frappe)
    stub = sys.modules["saas_core.billing.entitlements"]
    stub.can_change_plan = lambda u, t, p: (True, "")
    stub.plan_change_kind = lambda o, n: "upgrade" if (frappe.db.get_value("Plan", n, "sort_order") or 0) > (frappe.db.get_value("Plan", o, "sort_order") or 0) else "downgrade"
    stub.disk_shrinks = lambda o, n: False
    _load("proration"); _load("refunds"); pc = _load("plan_change")
    ns.gw = FakeGw()
    reg = type(sys)("saas_core.gateways.registry"); reg.get_gateway = lambda *a, **k: ns.gw
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    gw_pkg = type(sys)("saas_core.gateways"); gw_pkg.__path__ = []; gw_pkg.registry = reg
    monkeypatch.setitem(sys.modules, "saas_core.gateways", gw_pkg)
    now = dt.datetime(2026, 1, 11, 6, 30)
    real_now = pc.proration.utc_now
    monkeypatch.setattr(pc.proration, "utc_now", lambda: now.replace(tzinfo=pc.proration.UTC))
    ns.t = h.mod("saas_core.api.v1.tenants"); ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    for n, so, amt in (("starter", 1, 15500), ("pro", 2, 31000)):
        frappe.get_doc({"doctype": "Plan", "name": n, "code": n, "template": "erp", "sort_order": so,
                        "prices": [{"interval": "month", "currency": "INR", "amount": amt, "gateway": "razorpay",
                                    "gateway_price_id": f"plan_{n}"}]}).insert()
    yield ns
    h.restore()


def mksub(f, plan, paid, **kw):
    return f.get_doc({"doctype": "Subscription", "name": "SUB-1", "tenant": "acme", "owner_user": "alice@x.com",
                      "status": "active", "plan": plan, "gateway": "razorpay", "gateway_sub_id": "sub_1",
                      "interval": "month", "currency": "INR", "period_start": START, "period_end": END,
                      "paid_amount": paid, "gateway_payment_id": "pay_1", **kw}).insert()


def test_upgrade_returns_payment_url_no_job(env):
    h.seed(env.f, plan="starter"); mksub(env.f, "starter", 15500)
    out = env.t.change_plan(tenant="acme", plan="pro")
    assert out["payment_required"] is True and out["payment_url"] == "https://rzp.io/i/y" and out["scheduled"] is False
    assert out["proration"]["amount_due_minor"] == 10000 and env.enqueued == []
    assert env.f.local.response["http_status_code"] == 202
    assert env.f.get_doc("Subscription", "SUB-1").plan_change_state == "awaiting_payment"


def test_downgrade_job_shaped_with_proration(env):
    h.seed(env.f, plan="pro"); mksub(env.f, "pro", 31000)
    out = env.t.change_plan(tenant="acme", plan="starter")
    assert out["scheduled"] is True and out["kind"] == "change_plan" and out["payment_required"] is False
    assert out["proration"]["credit_minor"] == 10000 and out["payment_url"] is None
    assert env.enqueued == [("acme", "change_plan", None)]
    assert ("refund", "pay_1", 10000) in env.gw.calls


def test_no_price_falls_back_to_legacy(env):
    h.seed(env.f, plan="pro"); mksub(env.f, "pro", 31000)
    env.f.get_doc("Plan", "starter")["prices"] = []
    out = env.t.change_plan(tenant="acme", plan="starter")
    assert out["scheduled"] is True and "proration" not in out and env.gw.calls == []
    assert "effective_at" in str(env.f.get_doc("Provision Job", out["name"]).args)


def test_no_subscription_legacy_upgrade(env):
    h.seed(env.f, plan="starter")
    out = env.t.change_plan(tenant="acme", plan="pro")
    assert "scheduled" not in out and "proration" not in out and env.enqueued == [("acme", "change_plan", None)]
