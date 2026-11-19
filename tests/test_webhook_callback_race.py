"""Real Razorpay ordering: payment.captured webhook can beat the browser callback (and the Subscription).
Activation must not depend on arrival order: one Subscription, one Provision Job, no event stuck."""
import datetime as dt
import hashlib
import hmac
import importlib
import json
import sys
import types

import pytest

SECRET, WH = "key_secret_xyz", "whsec_x"
T0 = dt.datetime(2026, 1, 1)


def sig(msg, key):
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def mk(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


@pytest.fixture
def env(frappe, monkeypatch):
    from saas_core.gateways.razorpay import RazorpayGateway
    gw = RazorpayGateway(key_id="rzp_test_abc", key_secret=SECRET, webhook_secret=WH,
                         http=types.SimpleNamespace(request=lambda m, url, **k: {"id": "pay_X", "amount": 49900, "currency": "INR"}))
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: gw
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
        jobs.append(tenant)
        mk(frappe, "Provision Job", tenant=tenant, kind=kind, state="pending")
        frappe.db.set_value("Tenant", tenant, "status", "pending")
        return f"PJ-{len(jobs)}"
    jm.enqueue_job = enqueue_job
    for k, m in (("saas_core.gateways.registry", reg), ("saas_core.permissions", perm), ("saas_core.jobs", jm)):
        monkeypatch.setitem(sys.modules, k, m)
    frappe.session.user = "a@x.io"
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0,
       prices=[{"interval": "month", "amount": 49900, "currency": "INR", "gateway": "razorpay", "gateway_price_id": ""}])
    mk(frappe, "User", name="a@x.io", email="a@x.io")
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="a@x.io", status="pending", plan="pro")
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "sub_1", tenant="acme", owner_user="a@x.io", plan="pro", interval="month",
                  currency="INR", billing_mode="subscription")
    return types.SimpleNamespace(frappe=frappe, gw=gw, jobs=jobs,
                                 verify=importlib.import_module("saas_core.api.v1.checkout_verify"),
                                 webhooks=importlib.import_module("saas_core.webhooks"))


def hook(env, event, body, eid="x"):
    raw = json.dumps({"event": event, "payload": body}).encode()
    return env.webhooks.ingest("razorpay", {"X-Razorpay-Signature": sig(raw.decode(), WH), "X-Razorpay-Event-Id": eid}, raw)


# real shape: the subscription payment entity carries invoice_id/order_id, NOT subscription_id
CAPTURED = {"payment": {"entity": {"id": "pay_X", "amount": 49900, "currency": "INR", "order_id": "order_9",
                                    "invoice_id": "inv_9", "notes": []}}}
SUB = {"id": "sub_1", "customer_id": "cust_1", "notes": {"tenant": "acme"}, "current_end": 1769904000}
ACTIVATED = {"subscription": {"entity": SUB}}
CHARGED = {"subscription": {"entity": SUB},
           "payment": {"entity": {"id": "pay_X", "amount": 49900, "currency": "INR", "invoice_id": "inv_9"}}}


def callback(env):
    return env.verify.verify_payment("razorpay", {"razorpay_payment_id": "pay_X", "razorpay_subscription_id": "sub_1",
                                                  "razorpay_signature": sig("pay_X|sub_1", SECRET)})


def drain(env, now=T0):
    """Run every queued processor job once (stub queue), like the worker would."""
    from saas_core.billing import processor
    q, env.frappe._store.enqueued = list(env.frappe._store.enqueued), []
    for m, kw in q:
        if m.endswith("process_payment_event"):
            processor.process_payment_event(kw["payment_event"], now=now)


def evs(env):
    return [d for (dt_, _), d in env.frappe._store.docs.items() if dt_ == "Payment Event"]


def assert_clean(env):
    f = env.frappe
    assert len(f.get_all("Subscription")) == 1 and f.get_all("Subscription")[0].status == "active"
    assert len(f.get_all("Provision Job")) == 1 and env.jobs == ["acme"]
    assert evs(env) and all(e.status in ("processed", "ignored") for e in evs(env)), [(e.name, e.status, e.error) for e in evs(env)]


LATER = T0 + dt.timedelta(minutes=30)


def test_captured_then_callback_then_activated_charged(env):
    assert hook(env, "payment.captured", CAPTURED) == {"status": "accepted"}
    drain(env)  # no subscription ref and no session match from the payment's own ids -> transient, retryable
    e = evs(env)[0]
    assert e.status == "received" and e.attempts == 1 and "subscription not found" in e.error
    assert env.frappe.get_all("Subscription") == []
    assert callback(env) == {"status": "accepted"}  # NOT "duplicate": activates directly from the session
    assert len(env.frappe.get_all("Subscription")) == 1 and env.jobs == ["acme"]
    drain(env, LATER)  # the webhook event is retried now that the sub exists
    assert hook(env, "subscription.activated", ACTIVATED, "a")["status"] == "accepted"
    assert hook(env, "subscription.charged", CHARGED, "c")["status"] == "accepted"
    drain(env, LATER)
    assert_clean(env)


def test_callback_first_then_captured_then_activated_charged(env):
    assert callback(env) == {"status": "accepted"}
    drain(env)
    assert hook(env, "payment.captured", CAPTURED)["status"] == "duplicate"  # same payment key already processed
    assert hook(env, "subscription.activated", ACTIVATED, "a")["status"] == "accepted"
    assert hook(env, "subscription.charged", CHARGED, "c")["status"] == "accepted"
    drain(env)
    assert_clean(env)


def test_captured_carrying_subscription_id_activates_without_callback(env):
    cap = {"payment": {"entity": dict(CAPTURED["payment"]["entity"], subscription_id="sub_1")}}
    hook(env, "payment.captured", cap)
    drain(env)  # resolved through the Checkout Session: no waiting for the callback
    assert len(env.frappe.get_all("Subscription")) == 1 and env.jobs == ["acme"]
    assert callback(env) == {"status": "accepted"}  # different kind than the processed webhook: idempotent re-run
    drain(env)
    assert_clean(env)


def test_gateway_lookup_resolves_subscription(env):
    env.gw._http = types.SimpleNamespace(request=lambda m, url, **k: (
        {"id": "pay_X", "invoice_id": "inv_9"} if "/payments/" in url else {"id": "inv_9", "subscription_id": "sub_1"}))
    hook(env, "payment.captured", CAPTURED)
    drain(env)
    assert len(env.frappe.get_all("Subscription")) == 1 and env.jobs == ["acme"]
    assert evs(env)[0].status == "processed"


def _site_local(utc_naive):
    """Frappe's modified/creation are site-local (IST in the stub); T0 is naive UTC."""
    return utc_naive + dt.timedelta(hours=5, minutes=30)


def test_backoff_sweep_and_needs_attention(env):
    from saas_core.billing import processor
    hook(env, "payment.captured", CAPTURED)
    e = evs(env)[0]
    now = T0
    seen = []
    for i in range(6):
        drain(env, now)
        seen.append((e.status, e.attempts))
        if e.status == "needs_attention":
            break
        # not due yet: sweep ignores it; once the backoff elapsed it re-enqueues
        assert processor.sweep_received_events(now) == 0
        now = e.next_retry_at
        assert processor.sweep_received_events(now) == 1
    assert seen[0] == ("received", 1) and seen[-1] == ("needs_attention", 6)
    assert [(x.status) for x in evs(env)] == ["needs_attention"]
    assert env.frappe.get_all("Subscription") == []


def test_sweep_picks_up_old_received_event_only(env):
    from saas_core.billing import processor
    hook(env, "subscription.activated", ACTIVATED, "a")
    env.frappe._store.enqueued.clear()  # job lost
    e = evs(env)[0]
    e.modified = _site_local(T0)
    assert processor.sweep_received_events(T0 + dt.timedelta(seconds=60)) == 0
    assert processor.sweep_received_events(T0 + dt.timedelta(seconds=121)) == 1
    drain(env)
    assert e.status == "processed"


def test_sweep_accepts_aware_now_and_converts_to_site_local(env):
    from saas_core.billing import processor
    hook(env, "subscription.activated", ACTIVATED, "a")
    env.frappe._store.enqueued.clear()
    e = evs(env)[0]
    e.modified = _site_local(T0)
    aware = lambda s: (T0 + dt.timedelta(seconds=s)).replace(tzinfo=dt.timezone.utc)
    assert processor.sweep_received_events(aware(60)) == 0
    assert processor.sweep_received_events(aware(121)) == 1
