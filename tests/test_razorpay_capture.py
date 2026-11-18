"""Razorpay order payments must be captured (authorized payments are auto-voided and cannot be refunded)."""
import hashlib, hmac, importlib, io, json, urllib.error

import pytest

SECRET = "key_secret_xyz"


def _cls(mod, name):
    return getattr(importlib.import_module(f"saas_core.gateways.{mod}"), name)


class Http:
    """Scripted: GET payment -> pay; POST capture -> capture result or raises."""
    def __init__(self, pay, capture=None, capture_exc=None):
        self.pay, self.capture, self.capture_exc, self.calls = pay, capture, capture_exc, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("/capture"):
            if self.capture_exc:
                raise self.capture_exc
            return self.capture or {"status": "captured"}
        return self.pay


def gw(http):
    return _cls("razorpay", "RazorpayGateway")(key_id="k", key_secret=SECRET, webhook_secret="wh", http=http)


def cb(pid="pay_1", order="order_1"):
    s = hmac.new(SECRET.encode(), f"{order}|{pid}".encode(), hashlib.sha256).hexdigest()
    return {"razorpay_payment_id": pid, "razorpay_order_id": order, "razorpay_signature": s}


AUTH = {"id": "pay_1", "status": "authorized", "captured": False, "amount": 49900, "currency": "INR", "order_id": "order_1"}


def test_capture_on_verify():
    h = Http(AUTH)
    ev = gw(h).verify_client_callback(cb())
    cap = [c for c in h.calls if c[1].endswith("/payments/pay_1/capture")]
    assert len(cap) == 1 and cap[0][0] == "POST"
    assert cap[0][2]["json_body"] == {"amount": 49900, "currency": "INR"}
    assert ev.raw["paid"] is True


def test_already_captured_no_capture_call():
    h = Http({**AUTH, "status": "captured", "captured": True})
    gw(h).verify_client_callback(cb())
    assert not [c for c in h.calls if c[1].endswith("/capture")]


def test_already_captured_400_is_success():
    GE = _cls("base", "GatewayError")
    exc = GE("x", status=400, code="BAD_REQUEST_ERROR", description="This payment has already been captured")
    ev = gw(Http(AUTH, capture_exc=exc)).verify_client_callback(cb())
    assert ev.raw["paid"] is True


def test_capture_failure_blocks_paid_event():
    GE = _cls("base", "GatewayError")
    exc = GE("x", status=400, code="BAD_REQUEST_ERROR", description="amount mismatch")
    with pytest.raises(GE):
        gw(Http(AUTH, capture_exc=exc)).verify_client_callback(cb())


def test_failed_payment_not_paid():
    GE = _cls("base", "GatewayError")
    with pytest.raises(GE):
        gw(Http({**AUTH, "status": "failed"})).verify_client_callback(cb())


def test_subscription_callback_not_captured():
    h = Http(AUTH)
    s = hmac.new(SECRET.encode(), b"pay_1|sub_1", hashlib.sha256).hexdigest()
    gw(h).verify_client_callback({"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1", "razorpay_signature": s})
    assert [c for c in h.calls if c[0] != "GET"] == []  # read-only fetch only; never a capture


def test_order_create_sets_payment_capture():
    CP = _cls("base", "CheckoutParams")
    h = Http({"id": "order_1"}, )
    h.pay = {"id": "order_1"}
    gw(h).create_checkout_session(CP(tenant="a", plan="p", gateway_price_id="", metadata={"mode": "order", "amount": 5, "currency": "INR"}))
    assert h.calls[0][2]["json_body"]["payment_capture"] == 1


def _webhook_req(event, status):
    body = json.dumps({"event": event, "payload": {"payment": {"entity": {
        "id": "pay_1", "order_id": "order_1", "status": status, "amount": 49900, "currency": "INR"}}}}).encode()
    return {"X-Razorpay-Signature": hmac.new(b"wh", body, hashlib.sha256).hexdigest()}, body


def _setup(frappe, http):
    registry = importlib.import_module("saas_core.gateways.registry")
    g = gw(http)
    registry.get_gateway = lambda name=None, *a, **k: g
    return importlib.import_module("saas_core.webhooks")


def test_capture_on_webhook_authorized(frappe, monkeypatch):
    h = Http(AUTH)
    wh = _setup(frappe, h)
    monkeypatch.setattr(wh.registry, "get_gateway", lambda *a, **k: gw(h))
    hd, b = _webhook_req("payment.authorized", "authorized")
    assert wh.ingest("razorpay", hd, b) == {"status": "ignored"}
    assert any(c[1].endswith("/payments/pay_1/capture") for c in h.calls)


def test_webhook_capture_failure_rejects(frappe, monkeypatch):
    GE = _cls("base", "GatewayError")
    h = Http(AUTH, capture_exc=GE("x", status=500))
    wh = _setup(frappe, h)
    monkeypatch.setattr(wh.registry, "get_gateway", lambda *a, **k: gw(h))
    hd, b = _webhook_req("order.paid", "authorized")
    with pytest.raises(Exception):
        wh.ingest("razorpay", hd, b)


def test_refund_of_authorized_payment_captures_first():
    refunds_h = Http({**AUTH}, )
    calls = []
    g = gw(refunds_h)
    g.refund_payment = lambda *a, **k: calls.append(("refund", [c[1] for c in refunds_h.calls])) or {"id": "rf", "status": "processed"}
    g.get_payment = lambda pid: {"amount": 49900, "amount_refunded": 0}
    g.list_payment_refunds = lambda pid: []
    refunds = importlib.import_module("saas_core.billing.refunds")
    from types import SimpleNamespace
    doc = _Doc(gateway_payment_id="pay_1", amount=100, currency="INR", tenant="t", subscription="s", name="R1", kind="cancel")
    sub = _Doc(name="s", tenant="t", refunded_amount=0)
    assert refunds.issue(doc, sub, g) is True
    assert calls and any(u.endswith("/payments/pay_1/capture") for u in calls[0][1])


class _Doc(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)
    def __getattr__(self, k):
        return self.get(k)
    def __setattr__(self, k, v):
        self[k] = v
    def save(self, **kw):
        pass


def test_refund_blocked_when_capture_fails():
    GE = _cls("base", "GatewayError")
    h = Http(AUTH, capture_exc=GE("x", status=400, description="nope"))
    g = gw(h)
    called = []
    g.refund_payment = lambda *a, **k: called.append(1) or {}
    g.get_payment = lambda pid: {"amount": 49900, "amount_refunded": 0}
    refunds = importlib.import_module("saas_core.billing.refunds")
    doc = _Doc(gateway_payment_id="pay_1", amount=100, tenant="t", subscription="s", name="R1")
    sub = _Doc(name="s", tenant="t")
    refunds.flag = lambda *a, **k: None
    assert refunds.issue(doc, sub, g) is False and not called and doc.status == "failed"


def test_gateway_error_includes_provider_body(monkeypatch):
    base = importlib.import_module("saas_core.gateways.base")
    body = json.dumps({"error": {"code": "BAD_REQUEST_ERROR", "description": "x" * 500, "source": "s"}}).encode()
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(body))
    monkeypatch.setattr(base.urllib.request, "urlopen", boom)
    with pytest.raises(base.GatewayError) as ei:
        base.UrllibHttp().request("POST", "https://api.razorpay.com/v1/payments/pay_1/capture",
                                  json_body={"amount": 1}, basic_auth=("k", "SECRETKEY"))
    m = str(ei.value)
    assert "BAD_REQUEST_ERROR" in m and "400" in m and len(ei.value.description) == 300
    assert "SECRETKEY" not in m and '"amount"' not in m


def test_backfill_capture_authorized(frappe, monkeypatch):
    backfill = importlib.import_module("saas_core.billing.backfill")
    h = Http(AUTH)
    registry = importlib.import_module("saas_core.gateways.registry")
    monkeypatch.setattr(registry, "get_gateway", lambda *a, **k: gw(h))
    frappe.get_doc({"doctype": "Subscription", "gateway": "razorpay", "status": "active",
                    "gateway_payment_id": "pay_1", "tenant": "t"}).insert()
    out = backfill.capture_authorized(dry_run=True)
    assert out[0]["action"] == "would_capture" and not any(c[1].endswith("/capture") for c in h.calls)
    out = backfill.capture_authorized(dry_run=False)
    assert out[0]["action"] == "captured" and any(c[1].endswith("/capture") for c in h.calls)
