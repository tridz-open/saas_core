"""F3: no silent INR defaults; refund cap compares like currencies only."""
import importlib
import sys
import types

import pytest

from saas_core.gateways.base import CheckoutParams, GatewayError
from saas_core.gateways.razorpay import RazorpayGateway


class Http:
    def __init__(self, res=None):
        self.calls, self.res = [], res or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.res


def gw(res=None):
    return RazorpayGateway(key_id="rzp_test_abc", key_secret="s", webhook_secret="wh", http=Http(res))


def test_order_requires_currency():
    with pytest.raises(GatewayError, match="currency"):
        gw().create_checkout_session(CheckoutParams(tenant="a", plan="p", gateway_price_id="",
                                                    metadata={"mode": "order", "amount": 5}))


def test_order_passes_currency_through():
    g = gw({"id": "order_1"})
    g.create_checkout_session(CheckoutParams(tenant="a", plan="p", gateway_price_id="",
                                             metadata={"mode": "order", "amount": 5, "currency": "usd"}))
    assert g.http.calls[0][2]["json_body"]["currency"] == "USD"


def test_capture_requires_currency():
    g = gw()
    for cur in (None, ""):
        with pytest.raises(GatewayError, match="currency"):
            g.capture_payment("pay_1", 100, cur)
    assert g.http.calls == []
    g.capture_payment("pay_1", 100, "EUR")
    assert g.http.calls[0][2]["json_body"]["currency"] == "EUR"


def test_ensure_captured_without_payment_currency_refuses_to_capture():
    g = gw({"id": "pay_1", "status": "authorized", "amount": 100})
    with pytest.raises(GatewayError, match="currency"):
        g.ensure_captured("pay_1")
    assert [c[0] for c in g.http.calls] == ["GET"]


def test_payment_link_requires_currency():
    with pytest.raises(GatewayError, match="currency"):
        gw().create_payment_link(amount=100, currency="", description="x", email=None, notes={})


def test_get_payment_reports_currency():
    g = gw({"amount": 100, "amount_refunded": 0, "currency": "usd"})
    assert g.get_payment("pay_1")["currency"] == "USD"


def test_sync_plans_row_without_currency_is_an_error_not_inr(frappe):
    mod = importlib.import_module("saas_core.gateways.razorpay_sync")
    from tests.test_razorpay_sync import FakeHttp, setup
    setup(frappe, prices=[{"interval": "month", "currency": "", "amount": 100, "gateway": "razorpay"}])
    http = FakeHttp()
    res = mod.sync_plans(http=http)
    assert http.calls == [] and res["synced"] == []
    assert res["errors"] and "no currency" in res["errors"][0]["error"]


def test_backfill_capture_without_payment_currency_errors(frappe, monkeypatch):
    frappe.get_doc({"doctype": "Subscription", "name": "SUB-1", "tenant": "acme", "owner_user": "u@x.com",
                    "plan": "pro", "gateway": "razorpay", "status": "active",
                    "gateway_payment_id": "pay_1"}).insert()
    calls = []

    class G:
        http = types.SimpleNamespace(request=lambda *a, **k: {"id": "pay_1", "status": "authorized", "amount": 100})
        _auth = lambda self: ("k", "s")
        capture_payment = lambda self, *a: calls.append(a)

    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name=None, account=None: G()
    monkeypatch.setitem(sys.modules, "saas_core.gateways.registry", reg)
    from saas_core.billing import backfill
    out = backfill.capture_authorized(dry_run=False)
    assert calls == [] and out[0]["action"] == "error" and "currency" in out[0]["error"]


# -- refund cap --------------------------------------------------------------------------------------------

class _Doc(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class PayGW:
    def __init__(self, **pay):
        self.pay = pay

    def get_payment(self, pid):
        return dict(self.pay)


def _cap(pay, **doc):
    from saas_core.billing import refunds
    d = _Doc(gateway_payment_id="pay_1", amount=100, name="R1", **doc)
    return refunds._cap_exceeded(PayGW(**pay), d)


def test_cap_like_currencies_within_and_over():
    assert _cap({"amount": 500, "amount_refunded": 0, "currency": "INR"}, currency="INR") is None
    assert "exceeds" in _cap({"amount": 500, "amount_refunded": 450, "currency": "INR"}, currency="INR")


def test_cap_refuses_currency_mismatch():
    r = _cap({"amount": 500, "amount_refunded": 0, "currency": "USD"}, currency="INR")
    assert r and "differs" in r


def test_cap_refuses_refund_without_currency():
    assert "no currency" in _cap({"amount": 500, "amount_refunded": 0, "currency": "INR"}, currency="")


def test_cap_ignores_nothing_when_earlier_refund_in_other_currency(frappe):
    frappe.get_doc({"doctype": "Refund", "name": "R0", "gateway_payment_id": "pay_1", "amount": 100,
                    "currency": "USD", "status": "processed"}).insert()
    r = _cap({"amount": 500, "amount_refunded": 0, "currency": "INR"}, currency="INR")
    assert r and "earlier refund" in r


def test_cap_counts_earlier_refund_in_same_currency(frappe):
    frappe.get_doc({"doctype": "Refund", "name": "R0", "gateway_payment_id": "pay_1", "amount": 450,
                    "currency": "INR", "status": "processed"}).insert()
    assert "exceeds" in _cap({"amount": 500, "amount_refunded": 0, "currency": "INR"}, currency="INR")
