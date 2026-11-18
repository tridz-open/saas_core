import importlib, json, pytest

# conftest evicts saas_core modules per test, so resolve classes lazily to keep identities consistent.
API = "https://api.razorpay.com/v1"


def _cls(mod, name):
    return getattr(importlib.import_module(f"saas_core.gateways.{mod}"), name)


def GatewayError_():
    return _cls("base", "GatewayError")


class FakeHttp:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw)); return self.res


def gw(res=None):
    return _cls('razorpay', 'RazorpayGateway')(key_id="rzp_test_abc", key_secret="sec", webhook_secret="wh", http=FakeHttp(res))


def test_refund_request_shape_and_normalization():
    g = gw({"id": "rfnd_1", "status": "processed", "amount": 500, "payment_id": "pay_1", "extra": 1})
    out = g.refund_payment("pay_1", 500)
    m, url, kw = g.http.calls[0]
    assert (m, url) == ("POST", f"{API}/payments/pay_1/refund")
    assert kw["basic_auth"] == ("rzp_test_abc", "sec")
    assert kw["json_body"] == {"amount": 500, "speed": "normal"}
    assert out == {"id": "rfnd_1", "status": "processed", "amount": 500, "payment_id": "pay_1"}


def test_refund_notes_receipt_passthrough():
    g = gw({"id": "rfnd_2", "status": "pending", "amount": 100})
    out = g.refund_payment("pay_2", 100, notes={"tenant": "acme"}, receipt="r-1")
    body = g.http.calls[0][2]["json_body"]
    assert body["notes"] == {"tenant": "acme", "refund_doc": "r-1"} and body["receipt"] == "r-1" and body["speed"] == "normal"
    assert out["payment_id"] == "pay_2" and out["status"] == "pending"


@pytest.mark.parametrize("amt", [0, -5, 1.5, "100", None, True])
def test_refund_bad_amount(amt):
    g = gw()
    with pytest.raises(GatewayError_()):
        g.refund_payment("pay_1", amt)
    assert g.http.calls == []


def test_update_subscription_plan():
    g = gw({"id": "sub_1"})
    assert g.update_subscription_plan("sub_1", "plan_2") == {"id": "sub_1"}
    m, url, kw = g.http.calls[0]
    assert (m, url) == ("PATCH", f"{API}/subscriptions/sub_1")
    assert kw["json_body"] == {"plan_id": "plan_2", "schedule_change_at": "cycle_end"}
    assert kw["basic_auth"] == ("rzp_test_abc", "sec")
    g.update_subscription_plan("sub_1", "plan_2", at_cycle_end=False)
    assert g.http.calls[1][2]["json_body"]["schedule_change_at"] == "now"


def test_invoice_payment_id():
    g = gw({"id": "inv_1", "payment_id": "pay_9"})
    assert g.invoice_payment_id("inv_1") == "pay_9"
    assert g.http.calls[0][:2] == ("GET", f"{API}/invoices/inv_1")
    assert gw({"id": "inv_2", "payment_id": None}).invoice_payment_id("inv_2") is None
    assert gw({"id": "inv_3"}).invoice_payment_id("inv_3") is None


@pytest.mark.parametrize("mod,name", [("stripe", "StripeGateway")])
def test_defaults_raise(mod, name):
    g = _cls(mod, name)(http=FakeHttp())
    with pytest.raises(GatewayError_(), match="does not support"):
        g.refund_payment("p", 1)
    with pytest.raises(GatewayError_(), match="does not support"):
        g.update_subscription_plan("s", "p")
    with pytest.raises(GatewayError_(), match="does not support"):
        g.invoice_payment_id("i")


def _refund_payload(event, status):
    return {"event": event, "payload": {"refund": {"entity": {
        "id": "rfnd_7", "payment_id": "pay_7", "amount": 2500, "currency": "INR", "status": status}}}}


@pytest.mark.parametrize("event,status", [("refund.processed", "processed"), ("refund.failed", "failed")])
def test_parse_refund_events(event, status):
    p = _refund_payload(event, status)
    ev = gw().parse_event(p)
    assert ev.kind == "refund" and ev.event_id == f"{event}:rfnd_7"
    assert ev.amount == 2500 and ev.currency == "INR" and ev.invoice_ref is None and ev.raw == p


def test_payment_events_unchanged():
    p = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1", "amount": 900, "currency": "INR"}}}}
    ev = gw().parse_event(p)
    assert ev.kind == "payment.succeeded" and ev.event_id == "pay_1" and ev.amount == 900


def test_webhook_signature_unaffected():
    import hashlib, hmac
    body = json.dumps(_refund_payload("refund.processed", "processed")).encode()
    sig = hmac.new(b"wh", body, hashlib.sha256).hexdigest()
    g = gw()
    assert g.verify_webhook({"X-Razorpay-Signature": sig}, body) is True
    assert g.verify_webhook({"X-Razorpay-Signature": "bad"}, body) is False


def test_manual_refund():
    g = _cls('manual', 'ManualGateway')()
    assert g.refund_payment("pay_1", 300) == {"id": "manual_refund_pay_1", "status": "processed",
                                              "amount": 300, "payment_id": "pay_1"}
    assert g.update_subscription_plan("s", "p") == {}


def test_list_payment_refunds_and_get_payment_shapes():
    g = gw({"entity": "collection", "count": 1, "items": [
        {"id": "rfnd_1", "status": "processed", "amount": 5, "payment_id": "pay_1", "receipt": "R-1",
         "notes": {"refund_doc": "R-1"}}]})
    out = g.list_payment_refunds("pay_1")
    m, url, kw = g.http.calls[0]
    assert m == "GET" and url.startswith(f"{API}/payments/pay_1/refunds") and kw["basic_auth"] == ("rzp_test_abc", "sec")
    assert out[0]["receipt"] == "R-1" and out[0]["notes"] == {"refund_doc": "R-1"} and out[0]["amount"] == 5
    g2 = gw({"id": "pay_1", "amount": 300000, "amount_refunded": 1000, "currency": None})
    assert g2.get_payment("pay_1") == {"amount": 300000, "amount_refunded": 1000, "currency": None}
    assert g2.http.calls[0][:2] == ("GET", f"{API}/payments/pay_1")
