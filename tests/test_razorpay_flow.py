import hashlib, hmac, json, pytest
from saas_core.gateways.base import CheckoutParams, GatewayError
from saas_core.gateways.razorpay import RazorpayGateway

SECRET = "key_secret_xyz"


class FakeHttp:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw)); return self.res


def gw(res=None):
    return RazorpayGateway(key_id="rzp_test_abc", key_secret=SECRET, webhook_secret="wh", http=FakeHttp(res))


def sig(msg):
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def test_razorpay_supports():
    assert {"subscription", "order"} <= set(gw().supports)


def test_razorpay_client_payload_subscription():
    g = gw({"id": "sub_1", "short_url": "https://x"})
    r = g.create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="plan_1", customer_email="a@b.co"))
    p = g.client_payload(r)
    assert p["provider"] == "razorpay" and p["key"] == "rzp_test_abc" and p["subscription_id"] == "sub_1"
    assert p["prefill"]["email"] == "a@b.co" and "order_id" not in p
    assert SECRET not in json.dumps(p)


def test_razorpay_client_payload_order():
    g = gw({"id": "order_1", "amount": 49900, "currency": "INR", "notes": {"tenant": "acme"}})
    r = g.create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="",
                                                 customer_email="a@b.co", metadata={"mode": "order", "amount": 499, "currency": "INR"}))
    m, url, kw = g.http.calls[-1]
    assert (m, url.endswith("/orders"), kw["json_body"]["amount"]) == ("POST", True, 49900)
    p = g.client_payload(r)
    assert p["order_id"] == "order_1" and p["mode"] == "order" and p["amount"] == 49900
    assert SECRET not in json.dumps(p)


def test_razorpay_order_requires_amount():
    with pytest.raises(GatewayError):
        gw().create_checkout_session(CheckoutParams(tenant="a", plan="p", gateway_price_id="", metadata={"mode": "order"}))


def test_razorpay_signature_subscription():
    g = gw()
    ok = {"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1", "razorpay_signature": sig("pay_1|sub_1")}
    e = g.verify_client_callback(ok)
    assert (e.kind, e.event_id, e.subscription_ref, e.gateway) == ("checkout.completed", "pay_1", "sub_1", "razorpay")
    with pytest.raises(GatewayError):
        g.verify_client_callback({**ok, "razorpay_signature": "00" * 32})
    with pytest.raises(GatewayError):
        g.verify_client_callback({**ok, "razorpay_payment_id": "pay_2"})


def test_razorpay_signature_order():
    g = gw({"status": "captured"})
    ok = {"razorpay_payment_id": "pay_1", "razorpay_order_id": "order_1", "razorpay_signature": sig("order_1|pay_1")}
    assert g.verify_client_callback(ok).kind == "checkout.completed"
    with pytest.raises(GatewayError):
        g.verify_client_callback({**ok, "razorpay_signature": sig("pay_1|order_1")})
    with pytest.raises(GatewayError):
        g.verify_client_callback({"razorpay_payment_id": "pay_1"})


@pytest.mark.parametrize("event,kind,eid", [("payment.captured", "payment.succeeded", "pay_1"),
                                            ("order.paid", "payment.succeeded", "pay_1"),
                                            ("subscription.pending", "payment.failed", "subscription.pending:pay_1")])
def test_razorpay_webhook_kinds(event, kind, eid):
    e = gw().parse_event({"event": event, "payload": {"payment": {"entity": {"id": "pay_1", "amount": 100, "currency": "INR"}}}},
                         {"X-Razorpay-Event-Id": "evt_7"})
    assert e.kind == kind and e.event_id == eid  # signed-body id, not the unsigned evt_7 header
