"""Reference gateway: every Gateway method implemented in memory, no network, no money.

Use it to learn the contract (read this file next to docs/gateways/CONTRACT.md), in unit tests, and in dev.
It is registered as "fake" but is NOT selectable unless the experimental flag is on:
    bench --site <site> set-config saas_core_experimental fake_gateway      (or `all`)
A Gateway Account with gateway="fake" is then usable; production sites leave the flag off.

State lives in the module-level STATE dict so separate instances (the registry builds one per call) share it.
Call `reset()` between tests.

Wire format (what a real adapter would translate from the provider):
- Webhook: JSON body, header `x-fake-signature` = HMAC-SHA256 hex of the raw body with the webhook secret.
  Body: {"id", "type", "subscription", "customer", "tenant", "amount", "currency", "period_end"}.
  `type` is one of FAKE_EVENT_TYPES (maps 1:1 to a NormalizedEvent kind); anything else is ignored.
- Client callback: {"payment_id", "session_ref", "signature"}; signature = HMAC-SHA256 hex over
  "<payment_id>|<session_ref>" with the key secret.
"""
from __future__ import annotations

import json

from .base import (CAPABILITIES, CheckoutParams, CheckoutResult, Gateway, GatewayError, NormalizedEvent,
                   from_ts, hmac_hex, lower_headers, safe_equal)

SIGNATURE_HEADER = "x-fake-signature"
FAKE_EVENT_TYPES = {
    "checkout.completed": "checkout.completed",
    "payment.succeeded": "payment.succeeded",
    "payment.failed": "payment.failed",
    "subscription.canceled": "subscription.canceled",
    "subscription.updated": "subscription.updated",
    "refund": "refund",
}

STATE: dict = {"sessions": {}, "payments": {}, "refunds": {}, "subscriptions": {}, "calls": []}


def reset() -> None:
    for k in STATE:
        STATE[k] = [] if k == "calls" else {}


def sign_webhook(secret: str, raw_body: bytes) -> dict:
    """Headers a valid fake webhook carries. For tests and the conformance kit."""
    return {SIGNATURE_HEADER: hmac_hex(secret, raw_body)}


def sign_callback(secret: str, payment_id: str, session_ref: str) -> str:
    return hmac_hex(secret, f"{payment_id}|{session_ref}".encode())


def _minor(amount) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise GatewayError("amount must be a positive integer (minor units)")
    return amount


class FakeGateway(Gateway):
    name = "fake"
    supports = frozenset(CAPABILITIES)  # everything: it is the reference for each capability

    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        meta = params.metadata or {}
        currency = str(meta.get("currency") or "").upper()
        if not currency:
            raise GatewayError("checkout currency missing")  # never defaulted
        amount = int(meta.get("amount_minor") or 0)
        if meta.get("mode") == "order":
            _minor(amount)
        ref = f"fake_cs_{params.tenant}_{params.plan}"  # deterministic: same inputs, same session
        STATE["sessions"][ref] = {"tenant": params.tenant, "plan": params.plan, "amount": amount,
                                  "currency": currency, "mode": meta.get("mode") or "subscription"}
        return CheckoutResult(url=f"https://fake.invalid/pay/{ref}", session_ref=ref,
                              raw={"id": ref, "mode": meta.get("mode") or "subscription", "amount": amount,
                                   "currency": currency, "plan": params.plan})

    def client_payload(self, session) -> dict:
        ref = getattr(session, "session_ref", "") or ""
        return {"provider": self.name, "key": self._key_id, "session_ref": ref}  # key_id is public

    def verify_client_callback(self, payload: dict) -> NormalizedEvent:
        pid, ref, sig = (str((payload or {}).get(k) or "") for k in ("payment_id", "session_ref", "signature"))
        if not (pid and ref and sig) or not self._key_secret \
                or not safe_equal(sign_callback(self._key_secret, pid, ref), sig):
            raise GatewayError("invalid_signature")
        sess = STATE["sessions"].get(ref) or {}
        STATE["payments"][pid] = {"amount": int(sess.get("amount") or 0), "amount_refunded": 0,
                                  "currency": sess.get("currency")}
        return NormalizedEvent(event_id=pid, gateway=self.name, kind="checkout.completed", subscription_ref=ref,
                               raw={"payment_id": pid, "session_ref": ref})  # only signed fields

    def portal_link(self, customer_ref: str, return_url: str) -> str:
        return f"https://fake.invalid/portal/{customer_ref}?return={return_url}"

    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        STATE["subscriptions"][sub_ref] = "cancel_at_period_end" if at_period_end else "canceled"  # idempotent

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        sig = lower_headers(headers).get(SIGNATURE_HEADER)
        if not sig or not self._webhook_secret:
            return False
        return safe_equal(hmac_hex(self._webhook_secret, raw_body), str(sig).strip())

    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        kind = FAKE_EVENT_TYPES.get(str(payload.get("type") or ""), "ignored")
        eid = str(payload.get("id") or "")
        if not eid:  # derive from the signed body so a replay still collapses
            import hashlib
            eid = "fake_" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
        try:
            amount = int(payload.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        return NormalizedEvent(event_id=eid, gateway=self.name, kind=kind,
                               subscription_ref=payload.get("subscription"), customer_ref=payload.get("customer"),
                               tenant_ref=payload.get("tenant"), amount=amount, currency=payload.get("currency"),
                               period_end=from_ts(payload.get("period_end")), raw=payload)

    # -- optional capabilities -------------------------------------------------------------------------
    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        _minor(amount_minor)
        if receipt:  # idempotent per receipt, like a provider honouring an idempotency key
            for r in STATE["refunds"].values():
                if r["receipt"] == receipt and r["payment_id"] == payment_id:
                    return {k: r[k] for k in ("id", "status", "amount", "payment_id")}
        pay = STATE["payments"].setdefault(payment_id, {"amount": amount_minor, "amount_refunded": 0, "currency": None})
        if pay["amount_refunded"] + amount_minor > pay["amount"]:
            raise GatewayError("refund exceeds the remaining payment balance")
        rid = f"fake_rf_{len(STATE['refunds']) + 1}"
        STATE["refunds"][rid] = {"id": rid, "status": "processed", "amount": amount_minor, "payment_id": payment_id,
                                 "receipt": receipt, "notes": dict(notes or {})}
        pay["amount_refunded"] += amount_minor
        return {"id": rid, "status": "processed", "amount": amount_minor, "payment_id": payment_id}

    def list_payment_refunds(self, payment_id: str) -> list[dict]:
        return [dict(r) for r in STATE["refunds"].values() if r["payment_id"] == payment_id]

    def get_payment(self, payment_id: str) -> dict:
        pay = STATE["payments"].get(payment_id)
        if pay is None:
            raise GatewayError("payment not found")
        return {"amount": pay["amount"], "amount_refunded": pay["amount_refunded"], "currency": pay["currency"]}

    def update_subscription_plan(self, sub_ref: str, plan_id: str, *, at_cycle_end: bool = True) -> dict:
        STATE["subscriptions"][sub_ref] = f"plan:{plan_id}"
        return {"id": sub_ref, "plan_id": plan_id, "schedule_change_at": "cycle_end" if at_cycle_end else "now"}

    def invoice_payment_id(self, invoice_id: str) -> str | None:
        return STATE.get("invoice_payments", {}).get(invoice_id)

    def create_payment_link(self, *, amount: int, currency: str, description: str, email: str | None,
                            notes: dict | None = None, callback_url: str = "", reference_id: str = "") -> dict:
        _minor(amount)
        if not currency:
            raise GatewayError("payment link currency missing")
        lid = f"fake_pl_{reference_id or len(STATE['sessions']) + 1}"
        return {"id": lid, "short_url": f"https://fake.invalid/link/{lid}", "amount": amount,
                "currency": str(currency).upper()}
