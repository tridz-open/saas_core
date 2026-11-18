"""R5: a refund call that times out is never blindly retried; webhooks match by receipt/notes; the cap honours
Razorpay's amount_refunded."""
import datetime as dt
import sys
import types

import pytest

NOW = dt.datetime(2026, 11, 18, 6, 0)


class FakeGW:
    """Razorpay-like: one payment, a refund store; can 'time out' after creating (the response is lost)."""
    def __init__(self, amount=300000, already_refunded=0):
        self.refunds, self.calls = [], []
        self.pay = {"amount": amount, "amount_refunded": already_refunded}
        self.timeout_after_create = False
        self.fail_list = False

    def refund_payment(self, pid, amount, *, notes=None, receipt=""):
        self.calls.append(("create", pid, amount, receipt, dict(notes or {})))
        r = {"id": f"rfnd_{len(self.refunds) + 1}", "status": "processed", "amount": amount, "payment_id": pid,
             "receipt": receipt, "notes": {**(notes or {})}}
        self.refunds.append(r)
        self.pay["amount_refunded"] += amount
        if self.timeout_after_create:
            raise TimeoutError("read timed out")
        return r

    def list_payment_refunds(self, pid):
        self.calls.append(("list", pid))
        if self.fail_list:
            raise TimeoutError("list timed out")
        return [dict(r) for r in self.refunds]

    def get_payment(self, pid):
        self.calls.append(("get", pid))
        return dict(self.pay)


@pytest.fixture
def env(frappe, monkeypatch):
    em = types.ModuleType("saas_core.events")
    em.publish = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "saas_core.events", em)
    for n, kw in (("Plan", dict(name="pro", code="pro")),
                  ("Tenant", dict(name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")),
                  ("Subscription", dict(name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway="razorpay",
                                        status="active", refunded_amount=0, gateway_payment_id="pay_1"))):
        frappe.get_doc({"doctype": n, **kw}).insert()
    return types.SimpleNamespace(f=frappe, gw=FakeGW(), sub=frappe.get_doc("Subscription", "SUB-1"))


def new_doc(env, amount=100000, **kw):
    from saas_core.billing import refunds
    return refunds.create_doc(env.sub, user="u@x.com", kind="cancel", amount=amount, currency="INR",
                              payment_id="pay_1", reason="t", now=NOW, **kw)


def test_create_carries_receipt_and_notes(env):
    from saas_core.billing import refunds
    d = new_doc(env)
    assert refunds.issue(d, env.sub, env.gw, NOW)
    c = [x for x in env.gw.calls if x[0] == "create"][0]
    assert c[3] == d.name and c[4]["refund_doc"] == d.name


def test_timeout_then_retry_makes_exactly_one_refund(env):
    from saas_core.billing import refunds
    d = new_doc(env)
    env.gw.timeout_after_create = True
    assert refunds.issue(d, env.sub, env.gw, NOW) is False       # response lost; Razorpay did create it
    assert d.status == "failed" and not d.get("gateway_refund_id")
    env.gw.timeout_after_create = False
    assert refunds.issue(d, env.sub, env.gw, NOW) is True        # retry (as cancellation.sweep_due does)
    assert len(env.gw.refunds) == 1
    assert d.gateway_refund_id == "rfnd_1" and d.status == "processed"
    assert env.sub.refunded_amount == 100000


def test_retry_does_not_create_when_lookup_fails(env):
    from saas_core.billing import refunds
    d = new_doc(env)
    env.gw.timeout_after_create = True
    refunds.issue(d, env.sub, env.gw, NOW)
    env.gw.fail_list = True
    assert refunds.issue(d, env.sub, env.gw, NOW) is False
    assert len(env.gw.refunds) == 1  # no blind second create


def test_webhook_before_commit_links_by_notes_and_receipt(env):
    from saas_core.billing import processor
    d = new_doc(env)
    d.status = "pending"
    d.save()
    ent = {"id": "rfnd_9", "payment_id": "pay_1", "amount": 100000, "status": "processed",
           "receipt": None, "notes": {"refund_doc": d.name}}
    p = {"event": "refund.processed", "payload": {"refund": {"entity": ent}}}
    processor._refund({"gateway": "razorpay"}, p, NOW)
    assert d.status == "processed" and d.gateway_refund_id == "rfnd_9"
    assert env.f.get_doc("Subscription", "SUB-1").refunded_amount == 100000


def test_webhook_falls_back_to_receipt(env):
    from saas_core.billing import processor
    d = new_doc(env)
    d.status = "pending"
    d.save()
    ent = {"id": "rfnd_8", "payment_id": "pay_1", "amount": 7, "status": "processed", "receipt": d.name, "notes": []}
    processor._refund({"gateway": "razorpay"}, {"event": "refund.processed", "payload": {"refund": {"entity": ent}}}, NOW)
    assert d.status == "processed" and d.gateway_refund_id == "rfnd_8"


def test_cap_holds_when_razorpay_already_refunded(env):
    from saas_core.billing import refunds
    env.gw = FakeGW(amount=300000, already_refunded=250000)  # refunded elsewhere (dashboard); no local row
    d = new_doc(env, amount=100000)
    assert refunds.issue(d, env.sub, env.gw, NOW) is False
    assert d.status == "failed" and "exceeds" in d.error
    assert not [c for c in env.gw.calls if c[0] == "create"]
    assert env.sub.refunded_amount == 0


def test_cap_allows_exact_remainder(env):
    from saas_core.billing import refunds
    env.gw = FakeGW(amount=300000, already_refunded=200000)
    d = new_doc(env, amount=100000)
    assert refunds.issue(d, env.sub, env.gw, NOW) is True
