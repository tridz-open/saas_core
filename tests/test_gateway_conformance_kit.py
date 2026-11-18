"""Runs the reusable conformance kit (saas_core/gateways/conformance.py) against razorpay (HTTP mocked), stripe and
fake, and checks the kit itself catches a dishonest gateway."""
import json
import time

import pytest

from saas_core.gateways import fake as fake_mod
from saas_core.gateways.base import Gateway, GatewayError, hmac_hex
from saas_core.gateways.conformance import GatewayConformance
from saas_core.gateways.fake import FakeGateway
from saas_core.gateways.razorpay import RazorpayGateway
from saas_core.gateways.stripe import StripeGateway

KEY_SECRET, WH_SECRET = "sk_test_x", "whsec_test"


class RazorpayFakeHttp:
    """Stateful stand-in for api.razorpay.com: payments, refunds (no server-side receipt dedupe, like the real API)."""

    def __init__(self):
        self.payments = {"pay_1": {"id": "pay_1", "amount": 49900, "currency": "INR", "status": "captured",
                                   "amount_refunded": 0}}
        self.refunds = []

    def request(self, method, url, **kw):
        path = url.split("/v1/", 1)[1]
        body = kw.get("json_body") or {}
        if method == "POST" and path == "orders":
            return {"id": "order_1", "amount": body["amount"], "currency": body["currency"]}
        if method == "POST" and path == "subscriptions":
            return {"id": "sub_1", "short_url": "https://rzp.io/i/x"}
        if method == "GET" and path.startswith("payments/") and "/" not in path[len("payments/"):]:
            return dict(self.payments[path.split("/")[1]])
        if method == "POST" and path.endswith("/refund"):
            pid = path.split("/")[1]
            r = {"id": f"rfnd_{len(self.refunds) + 1}", "status": "processed", "amount": body["amount"],
                 "payment_id": pid, "receipt": body.get("receipt"), "notes": body.get("notes") or {}}
            self.refunds.append(r)
            self.payments[pid]["amount_refunded"] += body["amount"]
            return r
        if method == "GET" and "/refunds" in path:
            pid = path.split("/")[1]
            return {"items": [r for r in self.refunds if r["payment_id"] == pid]}
        raise AssertionError(f"unmocked razorpay call {method} {url}")


class TestRazorpayConformance(GatewayConformance):
    def make_gateway(self, **o):
        kw = dict(key_id="rzp_test_abc", key_secret=KEY_SECRET, webhook_secret=WH_SECRET, http=RazorpayFakeHttp())
        kw.update(o)
        return RazorpayGateway(**kw)

    def sign_webhook(self, gw, raw_body):
        return {"X-Razorpay-Signature": hmac_hex(WH_SECRET, raw_body)}

    def event_samples(self):
        pay = {"id": "pay_1", "amount": 49900, "currency": "INR", "order_id": "order_1"}
        sub = {"id": "sub_1", "customer_id": "cust_1", "notes": {"tenant": "acme"}, "current_end": 1_800_000_000}
        return {
            "payment.succeeded": {"event": "payment.captured", "payload": {"payment": {"entity": pay}}},
            "payment.failed": {"event": "payment.failed", "payload": {"payment": {"entity": pay}}},
            "checkout.completed": {"event": "subscription.activated", "payload": {"subscription": {"entity": sub}}},
            "subscription.canceled": {"event": "subscription.cancelled", "payload": {"subscription": {"entity": sub}}},
            "refund": {"event": "refund.processed",
                       "payload": {"refund": {"entity": {"id": "rfnd_9", "amount": 100, "currency": "INR"}}}},
            "ignored": {"event": "payment.authorized", "payload": {"payment": {"entity": pay}}},
        }

    def unknown_event_payload(self):
        return {"event": "something.new", "payload": {}}

    def refundable_payment(self, gw):
        return "pay_1", 49900

    def valid_client_callback(self, gw):
        return {"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1",
                "razorpay_signature": hmac_hex(KEY_SECRET, b"order_1|pay_1")}


class TestStripeConformance(GatewayConformance):
    def make_gateway(self, **o):
        kw = dict(key_id="pk_test_x", key_secret=KEY_SECRET, webhook_secret=WH_SECRET,
                  http=type("H", (), {"request": lambda s, m, u, **k: {"id": "cs_1", "url": "https://stripe/x"}})())
        kw.update(o)
        return StripeGateway(**kw)

    def sign_webhook(self, gw, raw_body):
        ts = str(int(time.time()))
        return {"Stripe-Signature": f"t={ts},v1=" + hmac_hex(WH_SECRET, ts.encode() + b"." + raw_body)}

    def event_samples(self):
        sub = {"id": "sub_1", "customer": "cus_1", "metadata": {"tenant": "acme"}, "current_period_end": 1_800_000_000}
        inv = {"id": "in_1", "subscription": "sub_1", "customer": "cus_1", "amount_paid": 4900, "amount_due": 4900,
               "currency": "usd", "metadata": {"tenant": "acme"}}
        return {
            "checkout.completed": {"id": "evt_1", "type": "checkout.session.completed",
                                   "data": {"object": {"subscription": "sub_1", "customer": "cus_1",
                                                       "amount_total": 4900, "currency": "usd",
                                                       "metadata": {"tenant": "acme"}}}},
            "payment.succeeded": {"id": "evt_2", "type": "invoice.paid", "data": {"object": inv}},
            "payment.failed": {"id": "evt_3", "type": "invoice.payment_failed", "data": {"object": inv}},
            "subscription.canceled": {"id": "evt_4", "type": "customer.subscription.deleted", "data": {"object": sub}},
            "subscription.updated": {"id": "evt_5", "type": "customer.subscription.updated", "data": {"object": sub}},
            "ignored": {"id": "evt_6", "type": "charge.dispute.created", "data": {"object": {}}},
        }


class TestFakeConformance(GatewayConformance):
    @pytest.fixture(autouse=True)
    def _reset(self):
        fake_mod.reset()

    def make_gateway(self, **o):
        kw = dict(key_id="pk_test_x", key_secret=KEY_SECRET, webhook_secret=WH_SECRET)
        kw.update(o)
        return FakeGateway(**kw)

    def sign_webhook(self, gw, raw_body):
        return fake_mod.sign_webhook(WH_SECRET, raw_body)

    def event_samples(self):
        base = {"subscription": "sub_1", "customer": "cus_1", "tenant": "acme", "amount": 4900, "currency": "USD"}
        out = {k: {"id": f"evt_{k}", "type": k, **base} for k in fake_mod.FAKE_EVENT_TYPES}
        out["ignored"] = {"id": "evt_ign", "type": "nothing.relevant"}
        return out

    def refundable_payment(self, gw):
        fake_mod.STATE["payments"]["pay_f1"] = {"amount": 4900, "amount_refunded": 0, "currency": "USD"}
        return "pay_f1", 4900

    def valid_client_callback(self, gw):
        return {"payment_id": "pay_f1", "session_ref": "fake_cs_acme_pro",
                "signature": fake_mod.sign_callback(KEY_SECRET, "pay_f1", "fake_cs_acme_pro")}


# ---- the kit must catch a dishonest gateway ---------------------------------------------------------------
def test_kit_flags_declared_but_missing_capability():
    class OverClaim(FakeGateway):
        name = "overclaim"
        # Declares everything but never implements update_subscription_plan:
        update_subscription_plan = Gateway.update_subscription_plan

    class T(TestFakeConformance):
        def make_gateway(self, **o):
            return OverClaim(key_id="pk_test_x", key_secret=KEY_SECRET, webhook_secret=WH_SECRET)

    with pytest.raises(AssertionError, match="update_subscription_plan"):
        T().test_capability_honesty()


def test_kit_flags_undeclared_but_implemented_capability():
    class Under(FakeGateway):
        name = "under"
        supports = FakeGateway.supports - {"refund"}

    class T(TestFakeConformance):
        def make_gateway(self, **o):
            return Under(key_id="pk_test_x", key_secret=KEY_SECRET, webhook_secret=WH_SECRET)

    with pytest.raises(AssertionError, match="refund_payment"):
        T().test_capability_honesty()


def test_kit_flags_webhook_that_accepts_any_signature():
    class Lax(FakeGateway):
        name = "lax"

        def verify_webhook(self, headers, raw_body):
            return True

    class T(TestFakeConformance):
        def make_gateway(self, **o):
            return Lax(key_id="pk_test_x", key_secret=KEY_SECRET, webhook_secret=WH_SECRET)

    with pytest.raises(AssertionError):
        T().test_verify_webhook_rejects_tampered_body()


def test_fake_refund_over_balance_refused():
    fake_mod.reset()
    gw = FakeGateway()
    fake_mod.STATE["payments"]["p"] = {"amount": 100, "amount_refunded": 0, "currency": "USD"}
    gw.refund_payment("p", 100, receipt="a")
    with pytest.raises(GatewayError):
        gw.refund_payment("p", 1, receipt="b")
