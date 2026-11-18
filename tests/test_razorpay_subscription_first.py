"""Razorpay: subscriptions primary (callback = paid), one-time order + payment-link invoices as fallback."""
import datetime as dt
import hashlib
import hmac
import json
import sys
import types

import pytest

from saas_core.gateways.base import CheckoutParams, GatewayError
from saas_core.gateways.razorpay import KIND_MAP, RazorpayGateway

T0 = dt.datetime(2026, 1, 1)
SECRET = "key_secret_xyz"


def mk(frappe, dt_, **kw):
    return frappe.get_doc({"doctype": dt_, **kw}).insert()


def sig(msg):
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


class FakeHttp:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.res


def gw(res=None):
    return RazorpayGateway(key_id="rzp_test_abc", key_secret=SECRET, webhook_secret="wh", http=FakeHttp(res))


@pytest.fixture
def jobs(frappe, monkeypatch):
    calls = []
    m = types.ModuleType("saas_core.jobs")

    def enqueue_job(tenant, kind, *, args=None, approval=None, dedupe_key=None):
        calls.append((tenant, kind))
        mk(frappe, "Provision Job", tenant=tenant, kind=kind, state="pending")
        frappe.db.set_value("Tenant", tenant, "status", "pending")
        return f"PJ-{len(calls)}"
    m.enqueue_job = enqueue_job
    monkeypatch.setitem(sys.modules, "saas_core.jobs", m)
    return calls


def seed_tenant(frappe):
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0,
       prices=[{"interval": "month", "amount": 49900, "currency": "INR", "gateway": "razorpay", "gateway_price_id": ""}])
    mk(frappe, "User", name="u@x.com", email="u@x.com")
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="pending", plan="pro")


def event(frappe, eid, kind, payload, **kw):
    return mk(frappe, "Payment Event", name=f"razorpay:{eid}", event_key=f"razorpay:{eid}", gateway="razorpay",
              event_id=eid, kind=kind, status="received", tenant_ref="acme", raw_payload=json.dumps(payload), **kw).name


def run(name, now=T0):
    from saas_core.billing import processor
    processor.process_payment_event(name, now=now)


def subs(frappe):
    return frappe.get_all("Subscription")


# -- gateway ---------------------------------------------------------------------------------------------

def test_callback_marks_paid_subscription_and_order():
    g = gw()
    e = g.verify_client_callback({"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1",
                                  "razorpay_signature": sig("pay_1|sub_1")})
    assert e.raw["paid"] is True and e.raw["payment_ref"] == "pay_1"
    g = gw({"status": "captured"})
    e = g.verify_client_callback({"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1",
                                  "razorpay_signature": sig("order_1|pay_1")})
    assert e.raw["paid"] is True and e.invoice_ref == "order_1"
    with pytest.raises(GatewayError):
        g.verify_client_callback({"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1",
                                  "razorpay_signature": "00" * 32})


def test_kind_map_lifecycle():
    assert KIND_MAP["subscription.charged"] == "payment.succeeded"
    assert KIND_MAP["subscription.pending"] == "payment.failed"
    assert KIND_MAP["subscription.halted"] == "payment.failed"
    assert KIND_MAP["subscription.cancelled"] == "subscription.canceled"
    assert KIND_MAP["payment_link.paid"] == "payment.succeeded"


def test_create_payment_link_body_and_parse_paid():
    g = gw({"id": "plink_1", "short_url": "https://rzp.io/i/x"})
    out = g.create_payment_link(amount=49900, currency="INR", description="pro renewal", email="u@x.com",
                                notes={"tenant": "acme", "subscription": "SUB-00001"},
                                callback_url="https://app.test/billing", reference_id="SUB-00001-20260201")
    m, url, kw = g.http.calls[-1]
    b = kw["json_body"]
    assert (m, url.endswith("/payment_links"), out["id"]) == ("POST", True, "plink_1")
    assert b["amount"] == 49900 and b["notify"]["email"] is False and b["reminder_enable"] is True
    assert b["customer"] == {"email": "u@x.com"} and b["notes"]["tenant"] == "acme" and b["callback_url"]
    assert SECRET not in json.dumps(b)
    ev = g.parse_event({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "amount_paid": 49900, "notes": {"tenant": "acme"}}},
        "payment": {"entity": {"id": "pay_9", "amount": 49900, "currency": "INR"}}}}, {"X-Razorpay-Event-Id": "e1"})
    assert ev.kind == "payment.succeeded" and ev.invoice_ref == "plink_1" and ev.tenant_ref == "acme"
    with pytest.raises(GatewayError):
        gw().create_payment_link(amount=0, currency="INR", description="x", email=None, notes={})


# -- processor: callback provisioning --------------------------------------------------------------------

def test_paid_subscription_callback_provisions_once(frappe, jobs):
    seed_tenant(frappe)
    cb = {"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1", "razorpay_signature": "x",
          "paid": True, "notes": {"tenant": "acme"}}
    run(event(frappe, "pay_1", "checkout.completed", cb, subscription_ref="sub_1"))
    assert subs(frappe)[0].status == "active" and subs(frappe)[0].gateway_sub_id == "sub_1"
    assert jobs == [("acme", "provision")]
    # webhook subscription.charged for the same payment is dup-safe
    run(event(frappe, "wh1", "payment.succeeded", {"event": "subscription.charged"}, subscription_ref="sub_1"))
    assert len(jobs) == 1 and len(subs(frappe)) == 1


def test_callback_after_activated_webhook_still_provisions(frappe, jobs):
    seed_tenant(frappe)
    run(event(frappe, "wh0", "checkout.completed", {"event": "subscription.activated"}, subscription_ref="sub_1"))
    assert jobs == []  # webhook activation alone never provisions
    cb = {"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1", "razorpay_signature": "x"}
    run(event(frappe, "pay_1", "checkout.completed", cb, subscription_ref="sub_1"))
    assert jobs == [("acme", "provision")] and len(subs(frappe)) == 1


# -- fallback checkout -----------------------------------------------------------------------------------

class CheckoutGW:
    def __init__(self, fail_first=True):
        self.params, self.fail_first = [], fail_first

    def create_checkout_session(self, params):
        self.params.append(params)
        if self.fail_first and len(self.params) == 1:
            from saas_core.gateways.base import GatewayError as GE  # conftest purges modules per test
            raise GE("boom")
        return types.SimpleNamespace(session_ref="order_1", url="", raw={}, payment_mocked=False)

    def client_payload(self, res):
        raise NotImplementedError


@pytest.fixture
def co(frappe):
    holder = types.SimpleNamespace(gw=CheckoutGW())
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: holder.gw
    ent = types.ModuleType("saas_core.billing.entitlements")
    ent.can_create_tenant = lambda user, plan: (True, "")
    ent.capacity_open = lambda: True
    sys.modules["saas_core.gateways.registry"] = reg
    sys.modules["saas_core.billing.entitlements"] = ent
    frappe.session.user = "u@x.com"
    mk(frappe, "User", name="u@x.com", email="u@x.com")
    mk(frappe, "Gateway Account", name="rz", account_name="rz", gateway="razorpay", enabled=1, checkout_mode="subscription")
    mk(frappe, "Plan", name="pro", code="pro", title="Pro", is_public=1,
       prices=[{"interval": "month", "amount": 49950, "currency": "INR", "gateway": "razorpay", "gateway_price_id": "plan_1"}])
    return holder


REQ = dict(plan="pro", interval="month", success_url="https://app.test/ok", cancel_url="https://app.test/no",
           gateway="razorpay")


def test_subscription_failure_falls_back_to_order(co):
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, slug="newsite")
    first, second = co.gw.params
    assert first.metadata["mode"] == "subscription" and "billing_mode" not in first.metadata
    assert second.metadata["mode"] == "order" and second.metadata["billing_mode"] == "invoice"
    assert second.metadata["amount_minor"] == 49950
    assert out["fallback"] == "invoice" and out["session_id"] == "order_1"


def test_missing_plan_id_falls_back_without_calling_subscription(co, frappe):
    co.gw = CheckoutGW(fail_first=False)
    frappe._store.docs[("Plan", "pro")]["prices"][0]["gateway_price_id"] = ""
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, slug="newsite2")
    assert len(co.gw.params) == 1 and co.gw.params[0].metadata["mode"] == "order"
    assert out["fallback"] == "invoice"


def test_happy_path_has_no_fallback_and_other_gateways_do_not_fall_back(co, frappe):
    co.gw = CheckoutGW(fail_first=False)
    from saas_core.api.v1 import checkout
    assert "fallback" not in checkout.create_session(**REQ, slug="newsite3")
    co.gw = CheckoutGW()
    out = checkout.create_session(**{**REQ, "gateway": "stripe"}, slug="newsite4")
    assert out["error"]["code"] == "gateway_error"
    assert len(co.gw.params) == 1


# -- invoice mode lifecycle ------------------------------------------------------------------------------

def order_cb(**kw):
    return {"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1", "razorpay_signature": "x", "paid": True,
            "captured_amount": 49900, "captured_currency": "INR",
            "notes": {"tenant": "acme", "billing_mode": "invoice", "interval": "month", "currency": "INR",
                      "amount_minor": 49900, "plan": "pro"}, **kw}


def first_order(frappe):
    seed_tenant(frappe)
    # the server-side session record (written by create_session) is what carries interval/amount/mode now;
    # the callback body no longer does, and there is no "latest subscription of the tenant" fallback.
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "order_1", tenant="acme", owner_user="u@x.com", plan="pro", interval="month",
                  currency="INR", billing_mode="invoice", checkout_mode="order", amount_minor=49900)
    run(event(frappe, "pay_1", "checkout.completed", order_cb(), invoice_ref="order_1"))


def test_paid_order_creates_invoice_mode_subscription(frappe, jobs):
    first_order(frappe)
    s = subs(frappe)[0]
    assert (s.billing_mode, s.status, s.gateway_sub_id) == ("invoice", "active", "")
    assert s.period_end > T0 + dt.timedelta(days=27)
    inv = frappe.get_all("Invoice")
    assert len(inv) == 1 and inv[0].status == "paid" and inv[0].gateway_invoice_id == "order_1"
    assert inv[0].total_amount == 49900
    assert jobs == [("acme", "provision")]
    # order.paid / payment.captured echoes and a replayed callback change nothing
    run(event(frappe, "wh_o", "payment.succeeded", {"event": "order.paid"}))
    run(event(frappe, "pay_1b", "checkout.completed", order_cb(), invoice_ref="order_1"))
    assert len(subs(frappe)) == 1 and len(frappe.get_all("Invoice")) == 1 and len(jobs) == 1


def test_issue_due_invoices_then_paid_extends_period(frappe, jobs, monkeypatch):
    first_order(frappe)
    from saas_core.billing import processor
    g = gw({"id": "plink_1", "short_url": "https://rzp.io/i/x"})
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: g
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    monkeypatch.setattr(processor, "_return_url", lambda: "https://app.test/console/billing")
    s = subs(frappe)[0]
    due = s.period_end
    assert processor.issue_due_invoices(now=T0 + dt.timedelta(days=5)) == 0  # not due yet
    assert processor.issue_due_invoices(now=due + dt.timedelta(hours=1)) == 1
    assert processor.issue_due_invoices(now=due + dt.timedelta(hours=2)) == 0  # no duplicate link
    body = g.http.calls[-1][2]["json_body"]
    assert body["amount"] == 49900 and body["notes"] == {"tenant": "acme", "subscription": s.name}
    assert body["callback_url"] == "https://app.test/console/billing" and body["notify"]["email"] is False
    opn = [i for i in frappe.get_all("Invoice") if i.status == "open"]
    assert len(opn) == 1 and opn[0].pdf_url == "https://rzp.io/i/x" and opn[0].gateway_invoice_id == "plink_1"
    paid_at = due + dt.timedelta(hours=5)
    wh = {"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "amount_paid": 49900, "notes": {"tenant": "acme"}}},
        "payment": {"entity": {"id": "pay_2", "amount": 49900, "currency": "INR"}}}}
    run(event(frappe, "wh_pl", "payment.succeeded", wh, invoice_ref="plink_1"), now=paid_at)
    run(event(frappe, "wh_pl2", "payment.succeeded", wh, invoice_ref="plink_1"), now=paid_at)
    s = subs(frappe)[0]
    assert s.status == "active" and s.period_end > due + dt.timedelta(days=27)
    assert len([i for i in frappe.get_all("Invoice") if i.status == "paid"]) == 2
    assert [i for i in frappe.get_all("Invoice") if i.status == "open"] == []
    assert len(jobs) == 1


def test_unpaid_invoice_enters_dunning(frappe, jobs, monkeypatch):
    first_order(frappe)
    from saas_core.billing import processor
    g = gw({"id": "plink_1", "short_url": "https://rzp.io/i/x"})
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: g
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    monkeypatch.setattr(processor, "_return_url", lambda: "https://app.test/b")
    due = subs(frappe)[0].period_end
    processor.issue_due_invoices(now=due)
    processor.issue_due_invoices(now=due + dt.timedelta(hours=3))
    assert subs(frappe)[0].status == "active"
    processor.issue_due_invoices(now=due + dt.timedelta(days=2))
    s = subs(frappe)[0]
    assert s.status == "past_due" and s.first_failure_at is not None
    assert len(g.http.calls) == 1


def test_scheduler_registered():
    from saas_core import hooks
    assert "saas_core.billing.processor.issue_due_invoices" in hooks.scheduler_events["daily"]


def test_price_amount_no_currency_fallback(frappe):
    from saas_core.billing import processor
    first_order(frappe)
    s = frappe.get_doc("Subscription", subs(frappe)[0].name)
    assert processor._price_amount(s)[1] == "INR"
    s.currency = "USD"  # plan only has an INR row
    with pytest.raises(processor.PriceNotFound):
        processor._price_amount(s)
    s.currency = ""
    with pytest.raises(processor.PriceNotFound):
        processor._price_amount(s)


def test_issue_due_invoices_currency_mismatch_audited_not_crashing(frappe, jobs, monkeypatch):
    first_order(frappe)
    from saas_core.billing import processor
    g = gw({"id": "plink_1", "short_url": "https://rzp.io/i/x"})
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: g
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    monkeypatch.setattr(processor, "_return_url", lambda: "https://app.test/b")
    s = frappe.get_doc("Subscription", subs(frappe)[0].name)
    due = s.period_end
    s.currency = "USD"
    s.save(ignore_permissions=True)
    logged = []
    monkeypatch.setattr(frappe, "log_error", lambda *a, **k: logged.append(a), raising=False)
    assert processor.issue_due_invoices(now=due + dt.timedelta(hours=1)) == 0
    assert g.http.calls == [] or all("plink" not in str(c) for c in g.http.calls[-0:])
    assert [i for i in frappe.get_all("Invoice") if i.status == "open"] == []
    assert any(a.action == "invoice.issue_failed" and a.target == s.name for a in frappe.get_all("Audit Event"))
    assert logged


# -- F2: captured payment vs checkout session / invoice -------------------------------------------------------

def _mismatch_case(frappe, **cap):
    seed_tenant(frappe)
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "order_1", tenant="acme", owner_user="u@x.com", plan="pro", interval="month",
                  currency="INR", billing_mode="invoice", checkout_mode="order", amount_minor=49900)
    cb = order_cb(**{"captured_amount": 49900, "captured_currency": "INR", **cap})
    return event(frappe, "pay_1", "checkout.completed", cb, invoice_ref="order_1")


def _audits(frappe, action):
    return [a for a in frappe.get_all("Audit Event") if a.action == action]


def test_capture_match_activates(frappe, jobs):
    run(_mismatch_case(frappe))
    assert len(subs(frappe)) == 1 and jobs == [("acme", "provision")]
    assert _audits(frappe, "payment.mismatch") == []


@pytest.mark.parametrize("cap,field", [({"captured_amount": 100}, "amount"), ({"captured_currency": "USD"}, "currency")])
def test_capture_mismatch_blocks_activation(frappe, jobs, monkeypatch, cap, field):
    logged = []
    monkeypatch.setattr(frappe, "log_error", lambda *a, **k: logged.append(a), raising=False)
    name = _mismatch_case(frappe, **cap)
    run(name)
    assert subs(frappe) == [] and frappe.get_all("Invoice") == [] and jobs == []
    ev = frappe.get_doc("Payment Event", name)
    assert ev.status == "needs_attention" and "PaymentMismatch" in ev.error
    a = _audits(frappe, "payment.mismatch")
    assert len(a) == 1 and a[0].target == name and field in a[0].details
    assert logged


def test_capture_unverifiable_blocks_activation(frappe, jobs):
    seed_tenant(frappe)
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "order_1", tenant="acme", owner_user="u@x.com", plan="pro", interval="month",
                  currency="INR", billing_mode="invoice", checkout_mode="order", amount_minor=49900)
    cb = order_cb()
    cb.pop("captured_amount"), cb.pop("captured_currency")
    name = event(frappe, "pay_1", "checkout.completed", cb, invoice_ref="order_1")
    run(name)
    assert subs(frappe) == [] and jobs == []
    assert frappe.get_doc("Payment Event", name).status == "needs_attention"


def test_webhook_payment_link_paid_mismatch_does_not_mark_invoice_paid(frappe, jobs, monkeypatch):
    first_order(frappe)
    from saas_core.billing import processor
    g = gw({"id": "plink_1", "short_url": "https://rzp.io/i/x"})
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: g
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    monkeypatch.setattr(processor, "_return_url", lambda: "https://app.test/b")
    due = subs(frappe)[0].period_end
    processor.issue_due_invoices(now=due + dt.timedelta(hours=1))
    for eid, cur, amt in (("w_cur", "USD", 49900), ("w_amt", "INR", 100)):
        wh = {"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"id": "plink_1", "notes": {"tenant": "acme"}}},
            "payment": {"entity": {"id": "pay_" + eid, "amount": amt, "currency": cur}}}}
        n = event(frappe, eid, "payment.succeeded", wh, invoice_ref="plink_1")
        run(n, now=due + dt.timedelta(hours=5))
        assert frappe.get_doc("Payment Event", n).status == "needs_attention"
    assert [i.status for i in frappe.get_all("Invoice") if i.status == "open"] == ["open"]
    assert len(_audits(frappe, "payment.mismatch")) == 2


def test_gateway_verify_records_fetched_amount_and_currency():
    g = gw({"id": "pay_1", "amount": 49900, "currency": "INR", "status": "captured"})
    ev = g.verify_client_callback({"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1",
                                   "razorpay_signature": sig("pay_1|sub_1")})
    assert ev.raw["captured_amount"] == 49900 and ev.raw["captured_currency"] == "INR"


@pytest.mark.strict_experimental
def test_subscription_mode_gated_and_order_is_default(frappe):
    from saas_core.api.v1 import checkout
    assert checkout._checkout_mode("razorpay") == "order"
    mk(frappe, "Gateway Account", name="rz", account_name="rz", gateway="razorpay", enabled=1, checkout_mode="subscription")
    assert checkout._checkout_mode("razorpay") == "order"  # experimental, not enabled
    frappe.conf["saas_core_experimental"] = "razorpay_subscription"
    assert checkout._checkout_mode("razorpay") == "subscription"
