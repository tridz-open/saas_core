"""Razorpay gateway: HMAC-SHA256 hex webhook verification, event normalization, REST via injectable http."""
from __future__ import annotations

from .base import (CheckoutParams, CheckoutResult, Gateway, GatewayError, NormalizedEvent, from_ts,
                   hmac_hex, lower_headers, safe_equal)

API = "https://api.razorpay.com/v1"
KIND_MAP = {
    "subscription.charged": "payment.succeeded",
    "payment.failed": "payment.failed",
    "subscription.halted": "payment.failed",    # after 4 failed retries: feeds the dunning path
    "subscription.activated": "checkout.completed",
    "subscription.authenticated": "checkout.completed",
    "subscription.cancelled": "subscription.canceled",
    "subscription.completed": "subscription.canceled",  # all billing cycles finished: term over
    # TODO: subscription.paused -> "paused" once Subscription has a paused status and a processor handler;
    # until then it parses as "ignored" (the expiry sweep covers a subscription that stays unpaid).
    "subscription.pending": "payment.failed",   # charge failed, Razorpay retries daily
    "payment_link.paid": "payment.succeeded",   # invoice-mode renewal paid
    "payment.captured": "payment.succeeded",
    "order.paid": "payment.succeeded",
    "refund.processed": "refund",
    "refund.failed": "refund",
}


class RazorpayGateway(Gateway):
    name = "razorpay"
    supports = frozenset({"subscription", "order", "client_callback"})

    def _auth(self):
        return (self._key_id, self._key_secret)

    @staticmethod
    def _amount_minor(meta: dict) -> int:
        if meta.get("amount_minor") is not None:
            return int(meta["amount_minor"])
        return int(round(float(meta.get("amount") or 0) * 100))

    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        meta = params.metadata or {}
        extra = {"customer_email": params.customer_email, "plan": params.plan, "tenant": params.tenant}
        if meta.get("mode") == "order":
            amount = self._amount_minor(meta)  # server-side plan price, never from the browser
            if amount <= 0:
                raise GatewayError("order amount missing")
            notes = {"tenant": params.tenant, "plan": params.plan, **{k: v for k, v in meta.items() if k not in ("amount", "amount_minor", "mode")}}
            if not meta.get("currency"):
                raise GatewayError("order currency missing")  # from the plan price row; never defaulted
            body = {"amount": amount, "currency": str(meta["currency"]).upper(),
                    "receipt": f"{params.tenant}-{params.plan}"[:40], "notes": notes,
                    "payment_capture": 1}  # auto-capture; capture_payment/ensure_captured remain the guarantee
            res = self.http.request("POST", f"{API}/orders", json_body=body, basic_auth=self._auth())
            return CheckoutResult(url="", session_ref=res.get("id", ""),
                                  raw={**res, **extra, "mode": "order"})
        body = {
            "plan_id": params.gateway_price_id,
            "total_count": 120,
            "customer_notify": 1,
            "notes": {"tenant": params.tenant, "plan": params.plan, **params.metadata},
        }
        if params.trial_days:
            import time
            body["start_at"] = int(time.time()) + params.trial_days * 86400
        res = self.http.request("POST", f"{API}/subscriptions", json_body=body, basic_auth=self._auth())
        return CheckoutResult(url=res.get("short_url", ""), session_ref=res.get("id", ""),
                              raw={**res, **extra, "mode": "subscription"})

    def client_payload(self, session) -> dict:
        raw = getattr(session, "raw", None) or {}
        ref = getattr(session, "session_ref", "") or raw.get("id", "")
        mode = raw.get("mode") or ("order" if str(ref).startswith("order_") else "subscription")
        out = {"provider": "razorpay", "key": self._key_id, "mode": mode,
               "name": raw.get("brand") or "Subscription",
               "description": f"{raw.get('plan') or ''} plan".strip(),
               "prefill": {"email": raw.get("customer_email") or ""},
               "notes": raw.get("notes") if isinstance(raw.get("notes"), dict) else {}}
        out["order_id" if mode == "order" else "subscription_id"] = ref
        if mode == "order":
            out["amount"], out["currency"] = raw.get("amount"), raw.get("currency")
        return out

    def verify_client_callback(self, payload: dict) -> NormalizedEvent:
        pid = str(payload.get("razorpay_payment_id") or "")
        sig = str(payload.get("razorpay_signature") or "")
        sub, order = payload.get("razorpay_subscription_id"), payload.get("razorpay_order_id")
        if not pid or not sig or not (sub or order):
            raise GatewayError("invalid_signature")
        msg = f"{pid}|{sub}" if sub else f"{order}|{pid}"
        if not safe_equal(hmac_hex(self._key_secret, msg.encode()), sig.strip()):
            raise GatewayError("invalid_signature")
        captured = {}
        if order and not sub:
            # Order payments sit `authorized` until captured and Razorpay auto-voids them: capture now, and fail
            # the callback (no activation/provisioning) if that is not possible. Subscription payments auto-capture.
            try:
                captured = self.ensure_captured(pid) or {}
            except GatewayError as e:
                raise GatewayError(f"capture failed: {e}", status=e.status, code=e.code,
                                   description=e.description) from None
        # A valid signature over payment_id|subscription_id (or order_id|payment_id) proves the payment was
        # authorised/captured: for an immediate-start subscription the authorisation IS the first charge.
        # Only the signed fields are kept. Tenant, plan, interval, billing mode and period come from the
        # server-side Checkout Session record (or an API fetch), never from client-supplied notes.
        raw = {"razorpay_payment_id": pid, "razorpay_signature": sig.strip(), "paid": True, "payment_ref": pid}
        if sub:
            try:  # subscription payments auto-capture; read the amount/currency actually charged
                captured = self.http.request("GET", f"{API}/payments/{pid}", basic_auth=self._auth()) or {}
            except GatewayError:
                captured = {}  # unverifiable: the processor refuses to activate until the webhook confirms
        if captured.get("amount") is not None and captured.get("currency"):
            raw["captured_amount"], raw["captured_currency"] = int(captured["amount"]), captured["currency"]
        raw["razorpay_subscription_id" if sub else "razorpay_order_id"] = sub or order
        return NormalizedEvent(event_id=pid, gateway=self.name, kind="checkout.completed",
                               subscription_ref=sub or None, tenant_ref=None,
                               invoice_ref=order or None, raw=raw)

    def capture_payment(self, payment_id: str, amount_minor: int, currency: str) -> dict:
        """POST /v1/payments/{id}/capture. An "already been captured" 400 counts as success (idempotent).
        `currency` is required (no default): pass the payment's/plan's currency."""
        if not currency:
            raise GatewayError("capture currency missing")
        try:
            return self.http.request("POST", f"{API}/payments/{payment_id}/capture",
                                     json_body={"amount": int(amount_minor), "currency": currency},
                                     basic_auth=self._auth()) or {}
        except GatewayError as e:
            text = (e.description or str(e)).lower()
            if e.status == 400 and "already" in text and "captured" in text:
                return {"id": payment_id, "status": "captured"}
            raise

    def ensure_captured(self, payment_id: str) -> dict:
        """Fetch the payment; capture it when `authorized`. Returns the payment dict once captured, else raises."""
        pay = self.http.request("GET", f"{API}/payments/{payment_id}", basic_auth=self._auth()) or {}
        status = pay.get("status")
        if status == "authorized":
            res = self.capture_payment(payment_id, int(pay.get("amount") or 0), pay.get("currency"))
            if res.get("status") not in (None, "captured"):
                raise GatewayError(f"payment {payment_id} not captured (status {res.get('status')})")
            return {**pay, **res, "status": "captured"}
        if status in ("captured", "refunded") or pay.get("captured") is True:
            return pay
        raise GatewayError(f"payment {payment_id} not captured (status {status or 'unknown'})")

    def fetch_session(self, ref: str) -> dict:
        """Server-side lookup of the notes WE set at creation (GET /subscriptions|/orders/{id}); {} on failure."""
        kind = "orders" if str(ref).startswith("order_") else "subscriptions"
        res = self.http.request("GET", f"{API}/{kind}/{ref}", basic_auth=self._auth())
        notes = res.get("notes") if isinstance(res, dict) and isinstance(res.get("notes"), dict) else {}
        return {"tenant": notes.get("tenant"), "plan": notes.get("plan"), "interval": notes.get("interval"),
                "currency": notes.get("currency"), "user": notes.get("user"),
                "billing_mode": "invoice" if kind == "orders" else "subscription",
                "amount_minor": res.get("amount") if kind == "orders" else notes.get("amount_minor")}

    def payment_subscription_id(self, payment_id: str):
        """Server-side: which subscription a captured payment belongs to (payment -> invoice -> subscription).
        A subscription payment entity usually carries only invoice_id/order_id. None when unknown."""
        pay = self.http.request("GET", f"{API}/payments/{payment_id}", basic_auth=self._auth()) or {}
        if pay.get("subscription_id"):
            return pay["subscription_id"]
        if pay.get("invoice_id"):
            inv = self.http.request("GET", f"{API}/invoices/{pay['invoice_id']}", basic_auth=self._auth()) or {}
            return inv.get("subscription_id") or None
        return None

    def create_payment_link(self, *, amount: int, currency: str, description: str, email: str | None,
                            notes: dict, callback_url: str = "", reference_id: str = "") -> dict:
        """POST /v1/payment_links for an invoice-mode renewal. Razorpay reminds the customer; we mail separately."""
        if int(amount or 0) <= 0:
            raise GatewayError("payment link amount missing")
        if not currency:
            raise GatewayError("payment link currency missing")
        body = {"amount": int(amount), "currency": currency, "description": description,
                "customer": {"email": email} if email else {},
                "notify": {"email": False, "sms": False}, "reminder_enable": True, "notes": dict(notes or {})}
        if reference_id:
            body["reference_id"] = str(reference_id)[:40]
        if callback_url:
            body["callback_url"], body["callback_method"] = callback_url, "get"
        res = self.http.request("POST", f"{API}/payment_links", json_body=body, basic_auth=self._auth())
        if not res.get("id"):
            raise GatewayError("payment link creation failed")
        return res

    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        """POST /v1/payments/{id}/refund (normal speed). Returns {"id","status","amount","payment_id"}."""
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise GatewayError("refund amount must be a positive integer (minor units)")
        body: dict = {"amount": amount_minor, "speed": "normal"}
        body_notes = dict(notes or {})
        if receipt:
            body["receipt"] = str(receipt)
            body_notes.setdefault("refund_doc", str(receipt))  # lets a webhook/retry find our Refund doc
        if body_notes:
            body["notes"] = body_notes
        res = self.http.request("POST", f"{API}/payments/{payment_id}/refund", json_body=body,
                                basic_auth=self._auth()) or {}
        return {"id": res.get("id"), "status": res.get("status"),
                "amount": int(res.get("amount") or amount_minor),
                "payment_id": res.get("payment_id") or payment_id}

    def list_payment_refunds(self, payment_id: str) -> list[dict]:
        """GET /v1/payments/{id}/refunds -> [{"id","status","amount","payment_id","receipt","notes"}] (up to 100)."""
        res = self.http.request("GET", f"{API}/payments/{payment_id}/refunds?count=100",
                                basic_auth=self._auth()) or {}
        out = []
        for r in res.get("items") or []:
            notes = r.get("notes") if isinstance(r.get("notes"), dict) else {}
            out.append({"id": r.get("id"), "status": r.get("status"), "amount": int(r.get("amount") or 0),
                        "payment_id": r.get("payment_id") or payment_id, "receipt": r.get("receipt"), "notes": notes})
        return out

    def get_payment(self, payment_id: str) -> dict:
        """GET /v1/payments/{id} -> {"amount","amount_refunded","currency"} (minor units; currency may be None)."""
        res = self.http.request("GET", f"{API}/payments/{payment_id}", basic_auth=self._auth()) or {}
        return {"amount": int(res.get("amount") or 0), "amount_refunded": int(res.get("amount_refunded") or 0),
                "currency": (str(res["currency"]).upper() if res.get("currency") else None)}

    def order_payments(self, order_id: str) -> list[dict]:
        """GET /v1/orders/{id}/payments -> [{"id","status","amount"}] (minor units; `status` captured = paid)."""
        res = self.http.request("GET", f"{API}/orders/{order_id}/payments", basic_auth=self._auth()) or {}
        return [{"id": r.get("id"), "status": r.get("status"), "amount": int(r.get("amount") or 0)}
                for r in res.get("items") or []]

    def subscription_invoices(self, sub_ref: str) -> list[dict]:
        """GET /v1/invoices?subscription_id= -> [{"id","status","payment_id","amount_paid","order_id","paid_at"}]."""
        res = self.http.request("GET", f"{API}/invoices?subscription_id={sub_ref}&count=100",
                                basic_auth=self._auth()) or {}
        return [{"id": r.get("id"), "status": r.get("status"), "payment_id": r.get("payment_id"),
                 "amount_paid": int(r.get("amount_paid") or 0), "order_id": r.get("order_id"),
                 "paid_at": r.get("paid_at")} for r in res.get("items") or []]

    def update_subscription_plan(self, sub_ref: str, plan_id: str, *, at_cycle_end: bool = True) -> dict:
        """PATCH /v1/subscriptions/{id} with the new plan_id."""
        body = {"plan_id": plan_id, "schedule_change_at": "cycle_end" if at_cycle_end else "now"}
        return self.http.request("PATCH", f"{API}/subscriptions/{sub_ref}", json_body=body,
                                 basic_auth=self._auth()) or {}

    def invoice_payment_id(self, invoice_id: str) -> str | None:
        inv = self.http.request("GET", f"{API}/invoices/{invoice_id}", basic_auth=self._auth()) or {}
        return inv.get("payment_id") or None

    def portal_link(self, customer_ref: str, return_url: str) -> str:
        # Razorpay has no hosted customer portal; send customers to the app's billing page.
        return return_url

    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        self.http.request("POST", f"{API}/subscriptions/{sub_ref}/cancel",
                          json_body={"cancel_at_cycle_end": 1 if at_period_end else 0},
                          basic_auth=self._auth())

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        sig = lower_headers(headers).get("x-razorpay-signature")
        if not sig or not self._webhook_secret:
            return False
        return safe_equal(hmac_hex(self._webhook_secret, raw_body), str(sig).strip())

    @staticmethod
    def _event_id(event, payload, pay, sub, link) -> str:
        """Dedupe id derived from the HMAC-signed body only (the x-razorpay-event-id header is unsigned, so a
        replay with a fresh header must still collapse). Payment-level events share `<payment_id>` with the
        client callback, so both paths collapse to one Payment Event; other events are `<event>:<entity id>`."""
        if payload.get("id"):  # a top-level id inside the signed body is as trustworthy as the body
            return str(payload["id"])
        if pay.get("id") and event in ("payment.captured", "order.paid", "payment.authorized"):
            return str(pay["id"])
        ent = pay.get("id") or link.get("id") or sub.get("id")
        if ent:
            extra = [str(sub.get(k)) for k in ("paid_count", "current_end") if sub.get(k) is not None and not pay.get("id")]
            return ":".join([str(event), str(ent), *extra])
        import hashlib, json
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]
        return f"{event}:{digest}"

    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        h = lower_headers(headers)
        event = payload.get("event", "")
        inner = payload.get("payload") or {}
        if KIND_MAP.get(event) == "refund":
            rf = (inner.get("refund") or {}).get("entity") or {}
            return NormalizedEvent(
                event_id=f"{event}:{rf.get('id')}" if rf.get("id") else self._event_id(event, payload, {}, {}, {}),
                gateway=self.name, kind="refund", amount=int(rf.get("amount") or 0),
                currency=rf.get("currency"), invoice_ref=None, raw=payload)
        sub = (inner.get("subscription") or {}).get("entity") or {}
        pay = (inner.get("payment") or {}).get("entity") or {}
        link = (inner.get("payment_link") or {}).get("entity") or {}
        notes = sub.get("notes") or link.get("notes") or pay.get("notes") or {}
        if isinstance(notes, list):
            notes = {}
        event_id = self._event_id(event, payload, pay, sub, link)
        return NormalizedEvent(
            event_id=str(event_id),
            gateway=self.name,
            kind=KIND_MAP.get(event, "ignored"),
            subscription_ref=sub.get("id") or pay.get("subscription_id"),
            customer_ref=sub.get("customer_id") or pay.get("customer_id"),
            tenant_ref=notes.get("tenant"),
            amount=int(pay.get("amount") or link.get("amount_paid") or 0),
            currency=pay.get("currency"),
            period_end=from_ts(sub.get("current_end")),
            invoice_ref=link.get("id") or pay.get("invoice_id"),
            raw=payload,
        )
