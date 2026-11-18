"""Legacy (pre-#15) subscriptions: _held fallback, refund payment id fallback, backfill (local, gateway, idempotent)."""
import datetime as dt
import sys
import types

import pytest

END = dt.datetime(2026, 12, 1, 0, 0)


class FakeGW:
    def __init__(self):
        self.calls = []

    def order_payments(self, oid):
        self.calls.append(("order_payments", oid))
        return [{"id": "pay_failed", "status": "failed", "amount": 520000},
                {"id": "pay_ok", "status": "captured", "amount": 520000}]

    def subscription_invoices(self, sid):
        self.calls.append(("sub_invoices", sid))
        return [{"id": "inv_1", "status": "paid", "payment_id": "pay_sub", "amount_paid": 99900,
                 "order_id": "order_x", "paid_at": 1}]

    def get_payment(self, pid):
        self.calls.append(("get_payment", pid))
        return {"amount": 99900, "amount_refunded": 0}


@pytest.fixture
def env(frappe, monkeypatch):
    gw = FakeGW()
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name=None, account=None: gw
    audit = []
    am = types.ModuleType("saas_core.audit")
    am.log = lambda action, **kw: audit.append((action, kw))
    for n, m in (("saas_core.gateways.registry", reg), ("saas_core.audit", am)):
        monkeypatch.setitem(sys.modules, n, m)
    for n, kw in (("Plan", dict(name="pro", code="pro")),
                  ("Tenant", dict(name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro"))):
        frappe.get_doc({"doctype": n, **kw}).insert()
    return types.SimpleNamespace(f=frappe, gw=gw, audit=audit)


def mk_sub(env, **kw):
    base = dict(name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway="razorpay", status="active",
                interval="month", currency="INR", period_end=END, paid_amount=0)
    base.update(kw)
    return env.f.get_doc({"doctype": "Subscription", **base}).insert()


def mk_inv(env, **kw):
    base = dict(name="INV-00001", tenant="acme", subscription="SUB-1", status="paid", total_amount=520000,
                currency="INR", gateway="razorpay", gateway_invoice_id="order_abc")
    base.update(kw)
    return env.f.get_doc({"doctype": "Invoice", **base}).insert()


def test_held_legacy_zero_falls_back_to_invoice(env):
    from saas_core.billing import cancellation
    mk_inv(env)
    held = cancellation._held(mk_sub(env))
    assert held["paid"] == 520000 and held["currency"] == "INR" and held["start"] is not None


def test_held_real_zero_with_payment_id_stays_zero(env):
    from saas_core.billing import cancellation
    mk_inv(env)
    assert cancellation._held(mk_sub(env, gateway_payment_id="pay_1"))["paid"] == 0


def test_refund_base_payment_id_falls_back_to_invoice(env):
    from saas_core.billing import refunds
    mk_inv(env, gateway_payment_id="pay_inv")
    assert refunds.base_payment_id(mk_sub(env)) == "pay_inv"


def test_backfill_local_from_invoice(env):
    from saas_core.billing import backfill
    mk_inv(env, gateway_payment_id="pay_inv")
    mk_sub(env)
    res = backfill.legacy_subscriptions(dry_run=False, use_gateway=False)
    sub = env.f.get_doc("Subscription", "SUB-1")
    assert sub.gateway_payment_id == "pay_inv" and sub.paid_amount == 520000
    assert sub.period_start == dt.datetime(2026, 11, 1) and res[0]["applied"] and env.gw.calls == []
    assert env.audit[0][0] == "subscription.backfill_legacy"


def test_backfill_local_from_payment_event(env):
    from saas_core.billing import backfill
    mk_inv(env)
    mk_sub(env)
    env.f.get_doc({"doctype": "Payment Event", "gateway": "razorpay", "event_id": "e1", "kind": "checkout.completed",
                   "raw_payload": '{"razorpay_payment_id": "pay_ev", "razorpay_order_id": "order_abc"}'}).insert()
    backfill.legacy_subscriptions(dry_run=False, use_gateway=False)
    assert env.f.get_doc("Subscription", "SUB-1").gateway_payment_id == "pay_ev"


def test_patch_mode_skips_gateway_only_rows(env):
    from saas_core.billing import backfill
    mk_inv(env)
    mk_sub(env)
    res = backfill.legacy_subscriptions(dry_run=False, use_gateway=False)
    sub = env.f.get_doc("Subscription", "SUB-1")
    assert not sub.get("gateway_payment_id") and not sub.get("paid_amount")
    assert sub.period_start and "locally" in res[0]["error"] and env.gw.calls == []


def test_backfill_via_gateway_order(env):
    from saas_core.billing import backfill
    mk_inv(env)
    mk_sub(env)
    backfill.legacy_subscriptions(dry_run=False)
    sub = env.f.get_doc("Subscription", "SUB-1")
    assert sub.gateway_payment_id == "pay_ok" and sub.paid_amount == 520000
    assert env.f.get_doc("Invoice", "INV-00001").gateway_payment_id == "pay_ok"
    assert env.gw.calls == [("order_payments", "order_abc")]


def test_backfill_via_gateway_subscription(env):
    from saas_core.billing import backfill
    mk_inv(env, gateway_invoice_id="inv_1")
    mk_sub(env, gateway_sub_id="sub_9")
    backfill.legacy_subscriptions(dry_run=False)
    sub = env.f.get_doc("Subscription", "SUB-1")
    assert sub.gateway_payment_id == "pay_sub" and sub.paid_amount == 99900


def test_dry_run_changes_nothing_and_prints(env, capsys):
    from saas_core.billing import backfill
    mk_inv(env)
    mk_sub(env)
    res = backfill.legacy_subscriptions()
    assert res[0]["changes"]["gateway_payment_id"] == "pay_ok" and not res[0]["applied"]
    assert "pay_ok" in capsys.readouterr().out
    sub = env.f.get_doc("Subscription", "SUB-1")
    assert not sub.get("gateway_payment_id") and env.audit == []


def test_idempotent(env):
    from saas_core.billing import backfill
    mk_inv(env)
    mk_sub(env)
    backfill.legacy_subscriptions(dry_run=False)
    env.gw.calls.clear()
    assert backfill.legacy_subscriptions(dry_run=False) == []
    assert env.gw.calls == [] and len(env.audit) == 1


def test_razorpay_read_methods_shapes():
    from saas_core.gateways.razorpay import RazorpayGateway
    urls = []

    class H:
        def request(self, method, url, **kw):
            urls.append(url)
            if "/orders/" in url:
                return {"items": [{"id": "pay_1", "status": "captured", "amount": 5}]}
            return {"items": [{"id": "inv_1", "status": "paid", "payment_id": "pay_2", "amount_paid": 7}]}

    gw = RazorpayGateway.__new__(RazorpayGateway)
    gw._http = H()
    gw._key_id = gw._key_secret = "k"
    assert gw.order_payments("order_1") == [{"id": "pay_1", "status": "captured", "amount": 5}]
    assert gw.subscription_invoices("sub_1")[0]["payment_id"] == "pay_2"
    assert urls[0].endswith("/orders/order_1/payments") and "subscription_id=sub_1" in urls[1]
