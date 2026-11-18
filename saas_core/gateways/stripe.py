"""Stripe gateway: Stripe-Signature verification (injectable clock), event normalization, REST via injectable http."""
from __future__ import annotations

import time

from .base import (CheckoutParams, CheckoutResult, Gateway, NormalizedEvent, from_ts,
                   hmac_hex, lower_headers, safe_equal)

API = "https://api.stripe.com/v1"
TOLERANCE = 300


class StripeGateway(Gateway):
    name = "stripe"
    supports = frozenset({"subscription", "portal"})

    def client_payload(self, session) -> dict:
        url = session.get("url") if isinstance(session, dict) else getattr(session, "url", "")
        return {"provider": "stripe", "url": url or ""}

    def verify_client_callback(self, payload: dict):
        raise NotImplementedError("Stripe is redirect-based: payments are verified via webhook, not client callback")

    def __init__(self, *, clock=time.time, tolerance: int = TOLERANCE, **kw):
        super().__init__(**kw)
        self._clock = clock
        self.tolerance = tolerance

    def _hdr(self):
        return {"Authorization": f"Bearer {self._key_secret}"}

    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        meta = {"tenant": params.tenant, "plan": params.plan, **params.metadata}
        data = {
            "mode": "subscription",
            "line_items": [{"price": params.gateway_price_id, "quantity": 1}],
            "success_url": params.success_url,
            "cancel_url": params.cancel_url,
            "client_reference_id": params.tenant,
            "metadata": meta,
            "subscription_data": {"metadata": meta},
        }
        if params.customer_ref:
            data["customer"] = params.customer_ref
        elif params.customer_email:
            data["customer_email"] = params.customer_email
        if params.trial_days:
            data["subscription_data"]["trial_period_days"] = params.trial_days
        res = self.http.request("POST", f"{API}/checkout/sessions", headers=self._hdr(), data=data)
        return CheckoutResult(url=res.get("url", ""), session_ref=res.get("id", ""), raw=res)

    def portal_link(self, customer_ref: str, return_url: str) -> str:
        res = self.http.request("POST", f"{API}/billing_portal/sessions", headers=self._hdr(),
                                data={"customer": customer_ref, "return_url": return_url})
        return res.get("url", "")

    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        if at_period_end:
            self.http.request("POST", f"{API}/subscriptions/{sub_ref}", headers=self._hdr(),
                              data={"cancel_at_period_end": "true"})
        else:
            self.http.request("DELETE", f"{API}/subscriptions/{sub_ref}", headers=self._hdr())

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        sig = lower_headers(headers).get("stripe-signature")
        if not sig or not self._webhook_secret:
            return False
        ts, v1s = None, []
        for part in str(sig).split(","):
            k, _, v = part.strip().partition("=")
            if k == "t":
                ts = v
            elif k == "v1":
                v1s.append(v)
        if not ts or not v1s:
            return False
        try:
            if abs(self._clock() - int(ts)) > self.tolerance:
                return False
        except ValueError:
            return False
        expected = hmac_hex(self._webhook_secret, ts.encode() + b"." + raw_body)
        return any([safe_equal(expected, v) for v in v1s])

    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        etype = payload.get("type", "")
        obj = (payload.get("data") or {}).get("object") or {}
        meta = obj.get("metadata") or (obj.get("subscription_details") or {}).get("metadata") or {}
        ev = NormalizedEvent(event_id=str(payload.get("id", "")), gateway=self.name, kind="ignored",
                             tenant_ref=meta.get("tenant"), raw=payload)
        if etype == "checkout.session.completed":
            ev.kind = "checkout.completed"
            ev.subscription_ref = obj.get("subscription")
            ev.customer_ref = obj.get("customer")
            ev.tenant_ref = meta.get("tenant") or obj.get("client_reference_id")
            ev.amount = int(obj.get("amount_total") or 0)
            ev.currency = obj.get("currency")
        elif etype in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"):
            ev.kind = "payment.failed" if etype.endswith("failed") else "payment.succeeded"
            ev.subscription_ref = obj.get("subscription")
            ev.customer_ref = obj.get("customer")
            ev.invoice_ref = obj.get("id")
            ev.amount = int((obj.get("amount_due") if ev.kind == "payment.failed" else obj.get("amount_paid")) or 0)
            ev.currency = obj.get("currency")
            lines = (obj.get("lines") or {}).get("data") or []
            if lines:
                ev.period_end = from_ts((lines[0].get("period") or {}).get("end"))
        elif etype in ("customer.subscription.deleted", "customer.subscription.updated"):
            ev.kind = "subscription.canceled" if etype.endswith("deleted") else "subscription.updated"
            ev.subscription_ref = obj.get("id")
            ev.customer_ref = obj.get("customer")
            ev.currency = obj.get("currency")
            ev.period_end = from_ts(obj.get("current_period_end"))
        return ev
