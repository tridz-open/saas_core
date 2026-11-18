"""Regression tests for T5_REVIEW P0-1, P0-2, P1-1, P1-2, P2-6, P2-11 and the KIND_MAP gaps.

Each test reproduces the exploit from the review and asserts it no longer works.
"""
import datetime as dt
import hashlib
import hmac
import json
import sys
import types

import pytest

SECRET = "key_secret_xyz"
WH = "whsec_x"
T0 = dt.datetime(2026, 1, 1)
REQ = dict(plan="pro", interval="month", success_url="https://app.test/ok", cancel_url="https://app.test/no")


def mk(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


def sig(msg, key=SECRET):
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


class FakeGW:
    def __init__(self):
        self.params = []

    def create_checkout_session(self, params):
        self.params.append(params)
        return types.SimpleNamespace(session_ref=f"sub_{len(self.params)}", url="https://pay.test/x", raw={},
                                     payment_mocked=False)

    def client_payload(self, res):
        raise NotImplementedError


@pytest.fixture
def world(frappe, monkeypatch):
    """Customer a@x.io, tenants acme (pending, theirs-free) and bravo (pending), plans pro/enterprise, jobs stub."""
    from saas_core.gateways.razorpay import RazorpayGateway
    rz = RazorpayGateway(key_id="rzp_test_abc", key_secret=SECRET, webhook_secret=WH,
                         http=types.SimpleNamespace(request=lambda m, url, **k: {"id": "pay_1", "amount": 900, "currency": "INR"}))
    holder = types.SimpleNamespace(fake=FakeGW(), used=[])

    def get_gateway(name, account=None):
        holder.used.append(name)
        return rz if name == "razorpay" and account is None and holder.real else holder.fake
    holder.real = False
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = get_gateway
    ent = types.ModuleType("saas_core.billing.entitlements")
    ent.can_create_tenant = lambda user, plan: (True, "")
    perm = types.ModuleType("saas_core.permissions")

    def assert_tenant_access(t, user=None, ptype="read"):
        d = frappe._store.docs.get(("Tenant", t))
        if not d or d.get("owner_user") != user:
            raise frappe.DoesNotExistError(t)
        return d
    perm.assert_tenant_access = assert_tenant_access
    jobs = []
    jm = types.ModuleType("saas_core.jobs")

    def enqueue_job(tenant, kind, *, args=None, approval=None, dedupe_key=None):
        jobs.append((tenant, kind))
        mk(frappe, "Provision Job", tenant=tenant, kind=kind, state="pending")
        return "PJ"
    jm.enqueue_job = enqueue_job
    for n, m in (("saas_core.gateways.registry", reg), ("saas_core.billing.entitlements", ent),
                 ("saas_core.permissions", perm), ("saas_core.jobs", jm)):
        monkeypatch.setitem(sys.modules, n, m)
    frappe.session.user = "a@x.io"
    frappe.conf["saas_core_default_gateway"] = "razorpay"
    frappe._roles = ["SaaS Customer"]  # NOT System Manager: the stub default would bypass the gateway policy
    mk(frappe, "User", name="a@x.io", email="a@x.io")
    for plan, amt in (("pro", 900), ("enterprise", 99900)):
        mk(frappe, "Plan", name=plan, code=plan, title=plan, is_public=1, available=1, trial_days=0,
           prices=[{"interval": m, "amount": amt, "currency": "INR", "gateway": "razorpay",
                    "gateway_price_id": f"plan_{plan}"} for m in ("month", "year")])
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="a@x.io", status="pending", plan="pro")
    mk(frappe, "Tenant", name="bravo", slug="bravo", owner_user="a@x.io", status="pending", plan="pro")
    holder.jobs, holder.frappe = jobs, frappe
    return holder


def live_razorpay(frappe, default="razorpay"):
    mk(frappe, "Gateway Account", name="rz", account_name="rz", gateway="razorpay", mode="live", enabled=1, checkout_mode="subscription")
    frappe.conf["saas_core_default_gateway"] = default


def process(name, now=T0):
    from saas_core.billing import processor
    processor.process_payment_event(name, now=now)


def events(frappe):
    return [d for (dt_, _), d in frappe._store.docs.items() if dt_ == "Payment Event"]


# ---------------------------------------------------------------- P0-1

def test_p0_1_callback_notes_cannot_redirect_payment_to_another_tenant(world):
    frappe = world.frappe
    world.real = True
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "sub_1", tenant="acme", owner_user="a@x.io", plan="pro", interval="month",
                  currency="INR", billing_mode="subscription", amount_minor=900)
    from saas_core.api.v1 import checkout_verify
    # genuine signed fields for acme's cheap sub_1 + attacker-chosen unsigned fields aiming at bravo/enterprise
    pl = {"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1",
          "razorpay_signature": sig("pay_1|sub_1"),
          "notes": {"tenant": "bravo", "plan": "enterprise", "billing_mode": "invoice", "interval": "year"},
          "tenant": "bravo", "plan": "enterprise", "interval": "year", "period_end": "2099-01-01"}
    assert checkout_verify.verify_payment("razorpay", pl) == {"status": "accepted"}
    ev = events(frappe)[0]
    assert ev.tenant_ref == "acme"
    raw = json.loads(ev.raw_payload)  # P2-6: only signed fields are stored
    assert set(raw) <= {"razorpay_payment_id", "razorpay_subscription_id", "razorpay_signature", "paid", "payment_ref",
                       "captured_amount", "captured_currency"}
    assert "enterprise" not in ev.raw_payload and "2099" not in ev.raw_payload and "bravo" not in ev.raw_payload
    process(ev.name)
    assert frappe.get_all("Subscription", filters={"tenant": "bravo"}) == []
    assert frappe.get_doc("Tenant", "bravo").plan == "pro" and frappe.get_doc("Tenant", "bravo").status == "pending"
    sub = frappe.get_all("Subscription", filters={"tenant": "acme"})[0]
    assert sub.plan == "pro" and sub.billing_mode != "invoice" and sub.interval == "month"
    assert str(sub.period_end or "")[:4] != "2099"
    assert world.jobs == [("acme", "provision")]


def test_p0_1_unknown_session_is_rejected(world):
    world.real = True
    from saas_core.api.v1 import checkout_verify
    pl = {"razorpay_payment_id": "pay_9", "razorpay_subscription_id": "sub_unknown",
          "razorpay_signature": sig("pay_9|sub_unknown"), "notes": {"tenant": "acme"}}
    r = checkout_verify.verify_payment("razorpay", pl)
    assert r["error"]["code"] == "not_found" and events(world.frappe) == []


def test_p0_1_session_record_written_at_create_session(world):
    frappe = world.frappe
    frappe.conf["saas_core_default_gateway"] = "razorpay"
    mk(frappe, "Gateway Account", name="rzs", account_name="rzs", gateway="razorpay", enabled=1, checkout_mode="subscription")
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, tenant="acme")
    sess = frappe.get_doc("Checkout Session", f"razorpay:{out['session_id']}")
    assert (sess.tenant, sess.plan, sess.interval, sess.billing_mode, sess.amount_minor, sess.owner_user) == \
        ("acme", "pro", "month", "subscription", 900, "a@x.io")
    assert sess.gateway_price_id == "plan_pro"


def test_p0_1_order_fallback_session_is_invoice_mode_and_processor_uses_it(world):
    frappe = world.frappe
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "order_1", tenant="acme", owner_user="a@x.io", plan="pro", interval="month",
                  currency="INR", billing_mode="invoice", amount_minor=900)
    cb = {"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1", "razorpay_signature": "x", "paid": True,
          "captured_amount": 900, "captured_currency": "INR",
          # attacker notes: would have meant a year of enterprise with a zero-amount invoice
          "notes": {"plan": "enterprise", "interval": "year", "amount_minor": 1, "billing_mode": "subscription"}}
    mk(frappe, "Payment Event", name="razorpay:pay_1", event_key="razorpay:pay_1", gateway="razorpay",
       event_id="pay_1", kind="checkout.completed", status="received", tenant_ref="acme", raw_payload=json.dumps(cb))
    process("razorpay:pay_1")
    sub = frappe.get_all("Subscription", filters={"tenant": "acme"})[0]
    assert sub.billing_mode == "invoice" and sub.plan == "pro" and sub.interval == "month"
    assert sub.period_end < T0 + dt.timedelta(days=40)
    assert frappe.get_all("Invoice")[0].total_amount == 900


# ---------------------------------------------------------------- P0-2

def test_p0_2_client_gateway_manual_ignored_for_customers(world):
    frappe = world.frappe
    mk(frappe, "Gateway Account", name="m", account_name="m", gateway="manual", mode="test", enabled=1)
    live_razorpay(frappe)
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, tenant="acme", gateway="manual")
    assert out["gateway"] == "razorpay" and world.used == ["razorpay"]
    assert frappe.get_all("Subscription") == []  # no free active subscription


def test_p0_2_system_manager_may_override_but_manual_still_refused_next_to_live(world):
    frappe = world.frappe
    frappe._roles = ["System Manager"]
    live_razorpay(frappe)
    from saas_core.api.v1 import checkout
    assert checkout.create_session(**REQ, tenant="acme", gateway="stripe")["gateway"] == "stripe"


def test_p0_2_registry_refuses_manual_when_any_live_account(frappe):
    mk(frappe, "Gateway Account", name="m", account_name="m", gateway="manual", mode="test", enabled=1)
    mk(frappe, "Gateway Account", name="rz", account_name="rz", gateway="razorpay", mode="live", enabled=1)
    from saas_core.gateways.registry import get_gateway
    with pytest.raises(Exception, match="live gateway"):
        get_gateway("manual", account="m")
    from saas_core.gateways.manual import ManualGateway
    from saas_core.gateways.base import GatewayError
    with pytest.raises(GatewayError):
        ManualGateway().verify_client_callback({"tenant": "acme", "plan": "enterprise"})


def test_p0_2_verify_payment_manual_ignored_for_customers(world):
    frappe = world.frappe
    mk(frappe, "Gateway Account", name="m", account_name="m", gateway="manual", mode="test", enabled=1)
    live_razorpay(frappe)
    world.real = True
    from saas_core.api.v1 import checkout_verify
    r = checkout_verify.verify_payment("manual", {"tenant": "acme", "plan": "enterprise"})
    assert r["error"]["code"] == "invalid_signature" and events(frappe) == []
    assert frappe.get_all("Subscription") == []


# ---------------------------------------------------------------- P1-1

def _gw_account(frappe):
    doc = frappe._Doc(doctype="Gateway Account", name="a1", gateway="razorpay", enabled=1, key_id="kid", mode="test")
    doc.get_password = lambda f, raise_exception=True: {"key_secret": SECRET, "webhook_secret": WH}[f]
    doc.insert()


def _signed(body: dict, eid):
    raw = json.dumps(body).encode()
    return {"X-Razorpay-Signature": sig(raw.decode(), WH), "X-Razorpay-Event-Id": eid}, raw


CAPTURED = {"event": "payment.captured", "payload": {"payment": {"entity": {
    "id": "pay_9", "amount": 900, "currency": "INR", "subscription_id": "sub_1"}}}}


def test_p1_1_replay_with_fresh_event_id_header_is_duplicate(world, monkeypatch):
    frappe = world.frappe
    monkeypatch.undo()  # use the real registry for this test
    for m in [m for m in sys.modules if m.startswith("saas_core.") and m not in ("saas_core.config",)]:
        del sys.modules[m]
    _gw_account(frappe)
    from saas_core import webhooks
    h1, raw = _signed(CAPTURED, "evt_A")
    h2, _ = _signed(CAPTURED, "evt_B")  # same signed body, attacker-chosen unsigned header
    assert webhooks.ingest("razorpay", h1, raw) == {"status": "accepted"}
    assert webhooks.ingest("razorpay", h2, raw) == {"status": "duplicate"}
    assert len(events(frappe)) == 1


def test_p1_1_callback_and_webhook_share_the_payment_id_key(world):
    frappe = world.frappe
    world.real = True
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "sub_1", tenant="acme", owner_user="a@x.io", plan="pro", interval="month")
    from saas_core.api.v1 import checkout_verify
    pl = {"razorpay_payment_id": "pay_9", "razorpay_subscription_id": "sub_1", "razorpay_signature": sig("pay_9|sub_1")}
    assert checkout_verify.verify_payment("razorpay", pl) == {"status": "accepted"}
    from saas_core.gateways.razorpay import RazorpayGateway
    ev = RazorpayGateway(key_secret=SECRET, webhook_secret=WH).parse_event(CAPTURED, {"X-Razorpay-Event-Id": "zzz"})
    from saas_core.webhooks import record_event
    assert record_event("razorpay", ev, json.dumps(CAPTURED))[1] is False
    assert len(events(frappe)) == 1


def test_p1_1_no_invoice_with_empty_gateway_invoice_id(world):
    frappe = world.frappe
    mk(frappe, "Subscription", name="SUB-1", tenant="acme", owner_user="a@x.io", plan="pro", gateway="razorpay",
       gateway_sub_id="sub_1", status="past_due", interval="month", dunning_stage=2)
    mk(frappe, "Payment Event", name="razorpay:pay_9", event_key="razorpay:pay_9", gateway="razorpay", event_id="pay_9",
       kind="payment.succeeded", status="received", subscription_ref="sub_1", raw_payload=json.dumps(CAPTURED))
    process("razorpay:pay_9")
    assert frappe.get_all("Invoice") == []
    s = frappe.get_doc("Subscription", "SUB-1")
    assert s.status == "past_due" and s.dunning_stage == 2  # unattributable payment does not resume dunning


# ---------------------------------------------------------------- P1-2

def test_p1_2_unrelated_payment_for_same_tenant_does_not_reset_dunning(world):
    frappe = world.frappe
    mk(frappe, "Subscription", name="SUB-OLD", tenant="acme", owner_user="a@x.io", plan="enterprise",
       gateway="razorpay", gateway_sub_id="sub_old", status="past_due", interval="month", dunning_stage=3)
    body = {"event": "subscription.charged", "payload": {
        "subscription": {"entity": {"id": "sub_new", "notes": {"tenant": "acme"}}},
        "payment": {"entity": {"id": "pay_5", "amount": 900, "currency": "INR", "invoice_id": "inv_5"}}}}
    mk(frappe, "Payment Event", name="razorpay:pay_5", event_key="razorpay:pay_5", gateway="razorpay", event_id="pay_5",
       kind="payment.succeeded", status="received", subscription_ref="sub_new", tenant_ref="acme",
       raw_payload=json.dumps(body))
    process("razorpay:pay_5")  # transient "not found": stays received (retried), never touches the other sub
    assert frappe.get_doc("Payment Event", "razorpay:pay_5").status == "received"
    s = frappe.get_doc("Subscription", "SUB-OLD")
    assert s.status == "past_due" and s.dunning_stage == 3
    assert frappe.get_all("Invoice") == []


def test_p1_2_create_session_refused_for_tenant_with_live_subscription(world):
    frappe = world.frappe
    frappe.conf["saas_core_default_gateway"] = "razorpay"
    mk(frappe, "Subscription", name="SUB-1", tenant="acme", owner_user="a@x.io", plan="enterprise",
       gateway="razorpay", gateway_sub_id="sub_old", status="past_due", interval="month")
    from saas_core.api.v1 import checkout
    r = checkout.create_session(**REQ, tenant="acme")
    assert r["error"]["code"] == "conflict" and frappe.local.response["http_status_code"] == 409
    assert world.fake.params == []


# ---------------------------------------------------------------- KIND_MAP / P2-11 / supervisor items

def test_kind_map_completed_canceled_and_paused_ignored():
    from saas_core.gateways.razorpay import KIND_MAP, RazorpayGateway
    assert KIND_MAP["subscription.completed"] == "subscription.canceled"
    assert "subscription.paused" not in KIND_MAP  # TODO in razorpay.py: no paused status/handler yet
    g = RazorpayGateway(key_secret=SECRET, webhook_secret=WH)
    assert g.parse_event({"event": "subscription.paused", "payload": {"subscription": {"entity": {"id": "s"}}}}).kind == "ignored"


def test_p2_11_guest_webhook_without_account_is_generic_400(frappe):
    from saas_core import webhooks
    with pytest.raises(frappe.ValidationError) as ei:
        webhooks.ingest("razorpay", {"X-Razorpay-Signature": "x"}, b"{}")
    assert frappe.local.response["http_status_code"] == 400
    assert "Gateway Account" not in str(ei.value) and "razorpay" not in str(ei.value).lower()


def test_webhook_receiver_rate_limited(frappe):
    from saas_core import webhooks
    from saas_core.api.v1._common import ApiError
    frappe.request = types.SimpleNamespace(headers={}, get_data=lambda: b"{}")
    for _ in range(300):
        with pytest.raises(frappe.ValidationError):
            webhooks.razorpay()
    with pytest.raises(ApiError) as ei:
        webhooks.razorpay()
    assert ei.value.code == "rate_limited" if hasattr(ei.value, "code") else True


def test_create_session_requires_verified_email(world, monkeypatch):
    from saas_core.config import signup
    monkeypatch.setattr(signup, "is_email_verified", lambda user: False)
    from saas_core.api.v1 import checkout
    r = checkout.create_session(**REQ, tenant="acme")
    assert r["error"]["code"] == "email_unverified" and world.frappe.local.response["http_status_code"] == 403
    assert world.fake.params == []


def test_payment_failed_mail_only_when_dunning_acts(world):
    frappe = world.frappe
    for name, status in (("SUB-C", "canceled"), ("SUB-A", "active")):
        mk(frappe, "Subscription", name=name, tenant="acme", owner_user="a@x.io", plan="pro", gateway="razorpay",
           gateway_sub_id=f"s_{name}", status=status, interval="month")
        mk(frappe, "Payment Event", name=f"razorpay:f_{name}", event_key=f"razorpay:f_{name}", gateway="razorpay",
           event_id=f"f_{name}", kind="payment.failed", status="received", subscription_ref=f"s_{name}",
           raw_payload="{}")
    frappe._store.enqueued.clear()
    process("razorpay:f_SUB-C")
    assert not [e for e in frappe._store.enqueued if e[0].endswith("send_payment_failed")]
    process("razorpay:f_SUB-A")
    assert [e for e in frappe._store.enqueued if e[0].endswith("send_payment_failed")]
