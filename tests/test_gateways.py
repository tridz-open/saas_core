import hashlib, hmac, json, pytest

SEC = "whsec_topsecret"


def sign(body: bytes, secret=SEC):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeHttp:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw)); return self.res


def rz(**kw):
    from saas_core.gateways.razorpay import RazorpayGateway
    return RazorpayGateway(key_id="k", key_secret="s", webhook_secret=SEC, **kw)


def st(now=1_700_000_000, **kw):
    from saas_core.gateways.stripe import StripeGateway
    return StripeGateway(key_secret="sk", webhook_secret=SEC, clock=lambda: now, **kw)


def test_razorpay_signature():
    body = b'{"event":"x"}'
    g = rz()
    assert g.verify_webhook({"X-Razorpay-Signature": sign(body)}, body)
    assert not g.verify_webhook({"X-Razorpay-Signature": sign(body)}, body + b" ")
    assert not g.verify_webhook({"X-Razorpay-Signature": "00"}, body)
    assert not g.verify_webhook({}, body)


@pytest.mark.parametrize("event,kind", [
    ("subscription.charged", "payment.succeeded"), ("payment.failed", "payment.failed"),
    ("subscription.halted", "payment.failed"), ("subscription.activated", "checkout.completed"),
    ("subscription.authenticated", "checkout.completed"), ("subscription.cancelled", "subscription.canceled"),
    ("refund.created", "ignored")])
def test_razorpay_mapping(event, kind):
    p = {"event": event, "id": "fallback", "payload": {
        "subscription": {"entity": {"id": "sub_1", "customer_id": "cust_1", "current_end": 1700000000, "notes": {"tenant": "acme"}}},
        "payment": {"entity": {"id": "pay_1", "amount": 49900, "currency": "INR", "invoice_id": "inv_1"}}}}
    e = rz().parse_event(p, {"X-Razorpay-Event-Id": "evt_9"})
    assert (e.kind, e.event_id, e.subscription_ref, e.tenant_ref, e.amount, e.currency, e.invoice_ref) == \
        (kind, "fallback", "sub_1", "acme", 49900, "INR", "inv_1")
    assert e.period_end is not None
    # the unsigned X-Razorpay-Event-Id header was the dedupe id (replayable): now only the signed body counts
    assert rz().parse_event(p).event_id == "fallback"


def stripe_header(body, t, secret=SEC, extra=()):
    v = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return ",".join([f"t={t}", *[f"v1={x}" for x in extra], f"v1={v}"])


def test_stripe_signature_and_tolerance():
    body = b'{"id":"evt_1"}'
    now = 1_700_000_000
    g = st(now)
    assert g.verify_webhook({"Stripe-Signature": stripe_header(body, now)}, body)
    assert g.verify_webhook({"Stripe-Signature": stripe_header(body, now, extra=("deadbeef",))}, body)
    assert not g.verify_webhook({"Stripe-Signature": stripe_header(body, now)}, body + b"x")
    assert not g.verify_webhook({"Stripe-Signature": stripe_header(body, now, secret="other")}, body)
    assert g.verify_webhook({"Stripe-Signature": stripe_header(body, now - 299)}, body)
    assert not g.verify_webhook({"Stripe-Signature": stripe_header(body, now - 301)}, body)
    assert not g.verify_webhook({"Stripe-Signature": stripe_header(body, now + 301)}, body)
    assert not g.verify_webhook({"Stripe-Signature": "garbage"}, body)
    assert not g.verify_webhook({}, body)


def test_stripe_mapping():
    g = st()
    def ev(t, obj): return g.parse_event({"id": "evt_1", "type": t, "data": {"object": obj}})
    e = ev("checkout.session.completed", {"subscription": "sub_1", "customer": "cus_1", "client_reference_id": "acme", "amount_total": 100, "currency": "usd"})
    assert (e.kind, e.subscription_ref, e.customer_ref, e.tenant_ref, e.amount) == ("checkout.completed", "sub_1", "cus_1", "acme", 100)
    inv = {"id": "in_1", "subscription": "sub_1", "customer": "cus_1", "amount_paid": 500, "amount_due": 700, "currency": "usd",
           "lines": {"data": [{"period": {"end": 1700000000}}]}, "subscription_details": {"metadata": {"tenant": "acme"}}}
    for t in ("invoice.paid", "invoice.payment_succeeded"):
        e = ev(t, inv)
        assert (e.kind, e.amount, e.invoice_ref, e.tenant_ref) == ("payment.succeeded", 500, "in_1", "acme") and e.period_end
    e = ev("invoice.payment_failed", inv)
    assert (e.kind, e.amount) == ("payment.failed", 700)
    assert ev("customer.subscription.deleted", {"id": "sub_1"}).kind == "subscription.canceled"
    e = ev("customer.subscription.updated", {"id": "sub_1", "current_period_end": 1700000000})
    assert e.kind == "subscription.updated" and e.subscription_ref == "sub_1" and e.period_end
    assert ev("charge.refunded", {}).kind == "ignored"


def test_stripe_checkout_uses_injected_http():
    h = FakeHttp({"url": "https://pay", "id": "cs_1"})
    from saas_core.gateways.base import CheckoutParams
    from saas_core.gateways.stripe import StripeGateway
    g = StripeGateway(key_secret="sk", webhook_secret=SEC, http=h)
    r = g.create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="price_1", success_url="s", cancel_url="c"))
    assert (r.url, r.session_ref) == ("https://pay", "cs_1")
    g._http = FakeHttp({"url": "https://portal"})
    assert g.portal_link("cus_1", "r") == "https://portal"
    g.cancel_subscription("sub_1", True)
    assert g._http.calls[-1][2]["data"] == {"cancel_at_period_end": "true"}


def test_razorpay_calls_use_injected_http():
    from saas_core.gateways.base import CheckoutParams
    g = rz(http=FakeHttp({"short_url": "https://rzp", "id": "sub_1"}))
    r = g.create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="plan_1"))
    assert r.url == "https://rzp"
    g.cancel_subscription("sub_1", False)
    assert g.http.calls[-1][2]["json_body"] == {"cancel_at_cycle_end": 0}


@pytest.mark.strict_experimental
def test_registry_uses_get_password(frappe):
    import importlib; registry = importlib.import_module("saas_core.gateways.registry")
    doc = frappe._Doc(doctype="Gateway Account", name="acc", account_name="acc", gateway="stripe", enabled=1, key_id="kid")
    doc.get_password = lambda f, raise_exception=True: {"key_secret": "SK", "webhook_secret": SEC}[f]
    doc.insert()
    with pytest.raises(frappe.ValidationError, match="experimental"):
        registry.get_gateway("stripe")
    frappe.conf["saas_core_experimental"] = "stripe"
    g = registry.get_gateway("stripe")
    assert g.name == "stripe" and g._webhook_secret == SEC
    with pytest.raises(frappe.ValidationError):
        registry.get_gateway("nope")
    with pytest.raises(frappe.ValidationError):
        registry.get_gateway("razorpay", "acc")
