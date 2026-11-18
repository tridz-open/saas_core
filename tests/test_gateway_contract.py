"""Shared contract every registered gateway must satisfy (GOAL_PLAN section 2, node G2)."""
import hashlib
import hmac
import json

import pytest
from test_billing_flow import mk, jobs  # noqa: F401  (jobs fixture)

KEY_SECRET = "sk_topsecret_value"
WH_SECRET = "whsec_topsecret_value"
NOW = 1_700_000_000
NAMES = ["razorpay", "stripe", "manual"]


class FakeHttp:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.res


def _hex(secret, msg):
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


# Canned provider responses for create_checkout_session.
RESPONSES = {
    "razorpay": {"id": "sub_rz1", "short_url": "https://rzp.io/i/x", "status": "captured"},
    "stripe": {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"},
}


def build(name):
    from saas_core.gateways.registry import GATEWAYS
    cls = GATEWAYS[name]
    kw = dict(key_id="key_pub", key_secret=KEY_SECRET, webhook_secret=WH_SECRET)
    if name == "stripe":
        kw["clock"] = lambda: NOW
    if name == "manual":
        kw["extra"] = {"mode": "test"}
    if name in RESPONSES:
        kw["http"] = FakeHttp(RESPONSES[name])
    elif name == "razorpay":
        kw["http"] = FakeHttp({"status": "captured", "captured": True})
    return cls(**kw)


def params():
    from saas_core.gateways.base import CheckoutParams
    return CheckoutParams(tenant="acme", plan="pro", gateway_price_id="price_1",
                          customer_email="u@x.com", success_url="https://ok", cancel_url="https://no")


def checkout(frappe, name, gw):
    if name == "manual":
        mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0)
        mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="pending", plan="pro")
        mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="test", enabled=1)
    return gw.create_checkout_session(params())


def test_registry_covers_expected_gateways(frappe):
    from saas_core.gateways.registry import GATEWAYS
    assert set(GATEWAYS) == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_supports_is_frozenset(frappe, name):
    gw = build(name)
    assert isinstance(gw.supports, frozenset) and gw.supports
    assert gw.name == name


@pytest.mark.parametrize("name", NAMES)
def test_create_checkout_session_result(frappe, jobs, name):
    from saas_core.gateways.base import CheckoutResult
    gw = build(name)
    res = checkout(frappe, name, gw)
    assert isinstance(res, CheckoutResult)
    assert isinstance(res.session_ref, str) and res.session_ref
    assert isinstance(res.url, str)


@pytest.mark.parametrize("name", NAMES)
def test_client_payload_shape_and_no_secrets(frappe, jobs, name):
    from saas_core.gateways.base import CheckoutResult
    gw = build(name)
    session = checkout(frappe, name, gw)
    payload = gw.client_payload(session)
    assert isinstance(payload, dict) and payload["provider"] == name
    blob = json.dumps(payload, default=str)
    assert KEY_SECRET not in blob and WH_SECRET not in blob
    assert not {"key_secret", "webhook_secret"} & set(payload)


def razorpay_callback(secret, sub=True, tamper=False):
    pid = "pay_1"
    if sub:
        payload = {"razorpay_payment_id": pid, "razorpay_subscription_id": "sub_1"}
        sig = _hex(secret, f"{pid}|sub_1".encode())
    else:
        payload = {"razorpay_payment_id": pid, "razorpay_order_id": "order_1"}
        sig = _hex(secret, f"order_1|{pid}".encode())
    payload["razorpay_signature"] = ("0" * 64) if tamper else sig
    return payload


@pytest.mark.parametrize("name", NAMES)
def test_verify_client_callback_contract(frappe, name):
    from saas_core.gateways.base import GatewayError, NormalizedEvent
    gw = build(name)
    if name == "razorpay":
        for sub in (True, False):
            ev = gw.verify_client_callback(razorpay_callback(KEY_SECRET, sub=sub))
            assert isinstance(ev, NormalizedEvent) and ev.kind == "checkout.completed"
            assert ev.gateway == "razorpay" and ev.event_id
            with pytest.raises(GatewayError):
                gw.verify_client_callback(razorpay_callback(KEY_SECRET, sub=sub, tamper=True))
        with pytest.raises(GatewayError):  # signed with the wrong secret
            gw.verify_client_callback(razorpay_callback("other", sub=True))
        return
    try:
        ev = gw.verify_client_callback({"tenant": "acme"})
    except (NotImplementedError, GatewayError):
        return
    assert isinstance(ev, NormalizedEvent) and ev.kind == "checkout.completed" and ev.gateway == name


def signed_sample(name):
    """(headers, raw_body, expected_event_id) for a webhook the gateway should accept."""
    if name == "razorpay":
        body = json.dumps({"event": "subscription.charged", "id": "evt_rz1", "payload": {
            "subscription": {"entity": {"id": "sub_1", "notes": {"tenant": "acme"}}},
            "payment": {"entity": {"id": "pay_1", "amount": 100, "currency": "INR"}}}}).encode()
        return {"X-Razorpay-Signature": _hex(WH_SECRET, body), "X-Razorpay-Event-Id": "evt_rz1"}, body, "evt_rz1"
    if name == "stripe":
        body = json.dumps({"id": "evt_st1", "type": "checkout.session.completed", "data": {"object": {
            "subscription": "sub_1", "customer": "cus_1", "client_reference_id": "acme"}}}).encode()
        sig = _hex(WH_SECRET, str(NOW).encode() + b"." + body)
        return {"Stripe-Signature": f"t={NOW},v1={sig}"}, body, "evt_st1"
    body = json.dumps({"id": "evt_mn1"}).encode()  # manual has no webhooks
    return {}, body, "evt_mn1"


@pytest.mark.parametrize("name", NAMES)
def test_webhook_verify_and_parse(frappe, name):
    from saas_core.gateways.base import NormalizedEvent
    gw = build(name)
    headers, body, event_id = signed_sample(name)
    if name == "manual":
        assert gw.verify_webhook(headers, body) is False
    else:
        assert gw.verify_webhook(headers, body) is True
        assert gw.verify_webhook(headers, body + b" ") is False  # tampered body
        assert gw.verify_webhook({}, body) is False  # unsigned
    ev = gw.parse_event(json.loads(body), headers)
    assert isinstance(ev, NormalizedEvent)
    assert ev.event_id == event_id and ev.gateway == name
    from saas_core.gateways.base import KINDS
    assert ev.kind in KINDS
