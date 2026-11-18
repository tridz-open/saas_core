"""Conformance kit: run it against your gateway to prove it honours the contract (docs/gateways/CONTRACT.md).

Usage in your app (tests/test_my_gateway.py):

    from saas_core.gateways.conformance import GatewayConformance

    class TestMyGateway(GatewayConformance):
        webhook_secret = "whsec_test"

        def make_gateway(self, **overrides):
            kw = dict(key_id="pk_test_x", key_secret="sk_test_x", webhook_secret=self.webhook_secret,
                      http=MyFakeHttp())
            kw.update(overrides)
            return MyGateway(**kw)

        def sign_webhook(self, gw, raw_body):          # headers a genuine webhook would carry
            return {"x-my-signature": hmac_hex(self.webhook_secret, raw_body)}

        def event_samples(self):                       # kind -> a provider payload that must parse to it
            return {"payment.succeeded": {...}, "ignored": {...}}

        # Only if you declare these capabilities:
        def refundable_payment(self, gw): ...          # -> (payment_id, amount_minor) the fake provider can refund
        def valid_client_callback(self, gw): ...       # -> payload verify_client_callback must accept

Run: `pytest -q tests/test_my_gateway.py`. No network: pass a fake `http` (see tests/test_gateway_conformance_kit.py).
The class name must start with `Test` so pytest collects it; `GatewayConformance` itself is not collected.
"""
from __future__ import annotations

import json

import pytest

from .base import (CAPABILITIES, CAPABILITY_METHODS, KINDS, CheckoutParams, CheckoutResult, Gateway, GatewayError,
                   NormalizedEvent)


class GatewayConformance:
    """Mixin of contract tests. Override the hooks below; every `test_*` method then runs against your gateway."""

    webhook_secret = "whsec_test"
    key_secret = "sk_test_x"

    # ---- hooks you implement -------------------------------------------------------------------------
    def make_gateway(self, **overrides) -> Gateway:
        """Return a gateway wired to a FAKE http client. `overrides` (e.g. webhook_secret="") replace constructor args."""
        raise NotImplementedError

    def sign_webhook(self, gw: Gateway, raw_body: bytes) -> dict:
        """Headers a genuine, correctly signed webhook with this body carries."""
        raise NotImplementedError

    def event_samples(self) -> dict:
        """{kind: provider_payload} with one payload per NormalizedEvent kind you emit. Must include "ignored"."""
        raise NotImplementedError

    # ---- optional hooks ------------------------------------------------------------------------------
    def checkout_params(self) -> CheckoutParams:
        return CheckoutParams(tenant="acme", plan="pro", gateway_price_id="price_1", customer_email="a@example.com",
                              success_url="https://app.example.com/ok", cancel_url="https://app.example.com/no",
                              metadata={"interval": "month", "currency": "USD", "amount_minor": 4900,
                                        "mode": "order"})

    def unknown_event_payload(self) -> dict:
        return {"id": "evt_unknown_1", "type": "__unknown__", "event": "__unknown__"}

    def refundable_payment(self, gw: Gateway):
        """-> (payment_id, amount_minor): a payment your fake provider will let you refund in full."""
        pytest.skip("refundable_payment() not implemented")

    def valid_client_callback(self, gw: Gateway) -> dict:
        pytest.skip("valid_client_callback() not implemented")

    # ---- helpers -------------------------------------------------------------------------------------
    @staticmethod
    def _body(payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True).encode()

    # ---- identity ------------------------------------------------------------------------------------
    def test_declares_name_and_supports(self):
        gw = self.make_gateway()
        assert isinstance(gw, Gateway)
        assert isinstance(gw.name, str) and gw.name, "set a non-empty class attribute `name`"
        assert isinstance(gw.supports, frozenset), "`supports` must be a frozenset"
        unknown = set(gw.supports) - set(CAPABILITIES)
        assert not unknown, f"unknown capabilities {sorted(unknown)}; allowed {sorted(CAPABILITIES)}"

    # ---- webhook signature ---------------------------------------------------------------------------
    def test_verify_webhook_accepts_genuine(self):
        gw = self.make_gateway()
        body = self._body(next(iter(self.event_samples().values())))
        assert gw.verify_webhook(self.sign_webhook(gw, body), body) is True

    def test_verify_webhook_rejects_tampered_body(self):
        gw = self.make_gateway()
        body = self._body(next(iter(self.event_samples().values())))
        headers = self.sign_webhook(gw, body)
        assert gw.verify_webhook(headers, body + b" ") is False

    def test_verify_webhook_rejects_missing_signature(self):
        gw = self.make_gateway()
        assert gw.verify_webhook({}, b"{}") is False

    def test_verify_webhook_rejects_bad_signature(self):
        gw = self.make_gateway()
        body = self._body(next(iter(self.event_samples().values())))
        headers = {k: "0" * len(str(v)) for k, v in self.sign_webhook(gw, body).items()}
        assert gw.verify_webhook(headers, body) is False

    def test_verify_webhook_rejects_wrong_secret(self):
        signer = self.make_gateway()
        body = self._body(next(iter(self.event_samples().values())))
        headers = self.sign_webhook(signer, body)
        assert self.make_gateway(webhook_secret="a-different-secret").verify_webhook(headers, body) is False

    def test_verify_webhook_rejects_when_no_secret_configured(self):
        signer = self.make_gateway()
        body = self._body(next(iter(self.event_samples().values())))
        headers = self.sign_webhook(signer, body)
        assert self.make_gateway(webhook_secret="").verify_webhook(headers, body) is False

    # ---- event normalisation -------------------------------------------------------------------------
    def test_event_samples_cover_ignored(self):
        samples = self.event_samples()
        assert "ignored" in samples, 'event_samples() must include an "ignored" payload'
        assert set(samples) <= set(KINDS), f"unknown kinds {sorted(set(samples) - set(KINDS))}"

    def test_parse_event_normalises_each_kind(self):
        gw = self.make_gateway()
        for kind, payload in self.event_samples().items():
            ev = gw.parse_event(payload, {})
            assert isinstance(ev, NormalizedEvent), kind
            assert ev.kind == kind, f"payload for {kind!r} parsed as {ev.kind!r}"
            assert ev.gateway == gw.name, kind
            assert isinstance(ev.event_id, str) and ev.event_id, f"{kind}: event_id must be a non-empty string"
            assert isinstance(ev.amount, int) and not isinstance(ev.amount, bool) and ev.amount >= 0, \
                f"{kind}: amount must be an int >= 0 in minor units"
            assert ev.currency is None or (isinstance(ev.currency, str) and len(ev.currency) == 3), \
                f"{kind}: currency must be a 3-letter ISO 4217 code or None"
            assert isinstance(ev.raw, dict), kind

    def test_parse_event_event_id_is_stable(self):
        gw = self.make_gateway()
        for kind, payload in self.event_samples().items():
            assert gw.parse_event(payload, {}).event_id == gw.parse_event(json.loads(json.dumps(payload)), {}).event_id, \
                f"{kind}: event_id must be derived only from the signed body"

    def test_parse_event_unknown_is_ignored_not_raised(self):
        gw = self.make_gateway()
        ev = gw.parse_event(self.unknown_event_payload(), {})
        assert ev.kind == "ignored"

    # ---- checkout ------------------------------------------------------------------------------------
    def test_checkout_result_shape(self):
        gw = self.make_gateway()
        res = gw.create_checkout_session(self.checkout_params())
        assert isinstance(res, CheckoutResult)
        assert isinstance(res.session_ref, str) and res.session_ref, "session_ref must be non-empty"
        assert isinstance(res.url, str)
        assert isinstance(res.raw, dict)

    def test_client_payload_has_provider_and_no_secrets(self):
        gw = self.make_gateway()
        payload = gw.client_payload(gw.create_checkout_session(self.checkout_params()))
        assert isinstance(payload, dict) and payload.get("provider") == gw.name
        blob = json.dumps(payload, default=str)
        for secret in (self.key_secret, self.webhook_secret):
            assert secret not in blob, "client_payload leaked a secret"

    def test_checkout_without_currency_is_refused(self):
        gw = self.make_gateway()
        params = self.checkout_params()
        params.metadata = {k: v for k, v in params.metadata.items() if k != "currency"}
        if "order" not in gw.supports:
            pytest.skip("gateway has no order mode")
        with pytest.raises(GatewayError):
            gw.create_checkout_session(params)

    # ---- client callback -----------------------------------------------------------------------------
    def test_client_callback_matches_capability(self):
        gw = self.make_gateway()
        if "client_callback" not in gw.supports:
            with pytest.raises(GatewayError):
                gw.verify_client_callback({})
            return
        ev = gw.verify_client_callback(self.valid_client_callback(gw))
        assert isinstance(ev, NormalizedEvent) and ev.kind == "checkout.completed" and ev.event_id
        with pytest.raises(GatewayError):
            gw.verify_client_callback({"garbage": "1"})

    # ---- refunds -------------------------------------------------------------------------------------
    def test_refund_rejects_bad_amounts(self):
        gw = self.make_gateway()
        if "refund" not in gw.supports:
            pytest.skip("no refund capability")
        pid, _ = self.refundable_payment(gw)
        for bad in (0, -5, True, 1.5, "100"):
            with pytest.raises(GatewayError):
                gw.refund_payment(pid, bad, receipt="R-bad")

    def test_refund_result_shape(self):
        gw = self.make_gateway()
        if "refund" not in gw.supports:
            pytest.skip("no refund capability")
        pid, amount = self.refundable_payment(gw)
        res = gw.refund_payment(pid, amount, receipt="R-shape")
        assert res["id"] and res["status"] in ("processed", "pending", "failed")
        assert res["amount"] == amount and isinstance(res["amount"], int) and res["payment_id"] == pid

    def test_refund_same_receipt_never_double_refunds(self):
        """Billing's retry rule: look the receipt up first and adopt a match; create only when absent.
        Gateways without refund_lookup must make refund_payment itself idempotent per receipt."""
        gw = self.make_gateway()
        if "refund" not in gw.supports:
            pytest.skip("no refund capability")
        pid, amount = self.refundable_payment(gw)
        receipt = "R-idem"
        first = gw.refund_payment(pid, amount, receipt=receipt)
        if "refund_lookup" in gw.supports:
            found = [r for r in gw.list_payment_refunds(pid) if r.get("receipt") == receipt]
            assert len(found) == 1, "list_payment_refunds must return the refund with the receipt it was created with"
            assert found[0]["id"] == first["id"] and int(found[0]["amount"]) == amount
        else:
            second = gw.refund_payment(pid, amount, receipt=receipt)
            assert second["id"] == first["id"], "without refund_lookup, refund_payment must be idempotent per receipt"

    def test_payment_lookup_shape(self):
        gw = self.make_gateway()
        if "payment_lookup" not in gw.supports:
            pytest.skip("no payment_lookup capability")
        pid, amount = self.refundable_payment(gw)
        pay = gw.get_payment(pid)
        assert isinstance(pay["amount"], int) and isinstance(pay["amount_refunded"], int)
        assert pay["amount"] >= amount >= 0 and pay["amount_refunded"] >= 0
        assert pay.get("currency") is None or len(str(pay["currency"])) == 3

    # ---- capability honesty --------------------------------------------------------------------------
    def test_capability_honesty(self):
        """Declared capability <=> method really overridden. Undeclared ones raise GatewayError, never AttributeError."""
        gw = self.make_gateway()
        for cap, method in CAPABILITY_METHODS.items():
            overridden = getattr(type(gw), method) is not getattr(Gateway, method)
            if cap in gw.supports:
                assert overridden, f"declares '{cap}' but does not override {method}()"
            else:
                assert not overridden, f"overrides {method}() but does not declare '{cap}' in supports"
                with pytest.raises(GatewayError):
                    self._call_unsupported(gw, method)

    @staticmethod
    def _call_unsupported(gw: Gateway, method: str):
        calls = {
            "verify_client_callback": lambda: gw.verify_client_callback({}),
            "refund_payment": lambda: gw.refund_payment("p_1", 100, receipt="r"),
            "list_payment_refunds": lambda: gw.list_payment_refunds("p_1"),
            "get_payment": lambda: gw.get_payment("p_1"),
            "update_subscription_plan": lambda: gw.update_subscription_plan("s_1", "plan_1"),
            "invoice_payment_id": lambda: gw.invoice_payment_id("inv_1"),
            "create_payment_link": lambda: gw.create_payment_link(amount=100, currency="USD", description="d",
                                                                  email=None),
        }
        return calls[method]()
