"""Manual (mock payment) gateway. Checkout completes in-process: no money moves, no external call.

Everything it creates carries payment_mocked=1. A manual Gateway Account in `live` mode is refused.
"""
from __future__ import annotations

from datetime import timedelta

import frappe

from saas_core.config import get_setting
from .base import CheckoutParams, CheckoutResult, Gateway, GatewayError, NormalizedEvent

DEFAULT_BANNER = "Test mode: payments mocked"


class ManualGateway(Gateway):
    name = "manual"
    supports = frozenset({"subscription", "client_callback", "refund", "plan_update"})

    def _is_test_mode(self) -> bool:
        from .policy import live_gateway_enabled
        if live_gateway_enabled():
            return False  # never mock payments next to a live gateway account
        mode = self.extra.get("mode")
        if mode:
            return mode == "test"
        try:
            rows = frappe.get_all("Gateway Account", filters={"gateway": "manual", "enabled": 1}, pluck="mode")
        except Exception:
            return False
        return bool(rows) and all(m == "test" for m in rows)

    def client_payload(self, session) -> dict:
        return {"provider": "manual"}

    def verify_client_callback(self, payload: dict) -> NormalizedEvent:
        if not self._is_test_mode():
            raise GatewayError("manual gateway client callbacks are only allowed in test mode")
        p = payload or {}
        tenant = p.get("tenant")
        return NormalizedEvent(event_id=str(p.get("id") or f"mock_cb_{tenant}"), gateway="manual",
                               kind="checkout.completed", tenant_ref=tenant,
                               subscription_ref=f"mock_sub_{tenant}" if tenant else None,
                               customer_ref=f"mock_cust_{tenant}" if tenant else None,
                               raw={"tenant": tenant})  # client payload is not stored

    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        from saas_core.billing.proration import naive_utc_now
        now = naive_utc_now()  # billing datetimes are naive UTC
        meta = params.metadata or {}
        tenant = frappe.get_doc("Tenant", params.tenant)
        interval = meta.get("interval") or "month"
        end = now + timedelta(days=365 if interval == "year" else 30)
        sub_id = f"mock_sub_{params.tenant}"
        rows = frappe.get_all("Subscription", filters={"tenant": params.tenant}, pluck="name", limit=1)
        if rows:
            sub = frappe.get_doc("Subscription", rows[0])
        else:
            sub = frappe.get_doc({"doctype": "Subscription", "tenant": params.tenant,
                                  "owner_user": tenant.get("owner_user"), "plan": params.plan,
                                  "interval": interval, "currency": meta.get("currency"),
                                  "dunning_stage": 0, "status": "active"})
            sub.insert(ignore_permissions=True)
        sub.gateway, sub.gateway_sub_id, sub.gateway_customer_id = "manual", sub_id, f"mock_cust_{params.tenant}"
        sub.plan, sub.status, sub.period_end, sub.payment_mocked = params.plan, "active", end, 1
        sub.last_event_at = now
        sub.save(ignore_permissions=True)
        inv = frappe.get_doc({"doctype": "Invoice", "tenant": params.tenant, "owner_user": tenant.get("owner_user"),
                              "subscription": sub.get("name"), "gateway": "manual",
                              "gateway_invoice_id": f"mock_inv_{params.tenant}_{now.strftime('%Y%m%d%H%M%S')}",
                              "status": "paid", "total_amount": 0, "currency": meta.get("currency"),
                              "issued_at": now, "paid_at": now, "period_end": end, "payment_mocked": 1})
        inv.insert(ignore_permissions=True)
        tenant.plan = params.plan
        tenant.save(ignore_permissions=True)
        if tenant.get("status") == "pending":
            import importlib
            importlib.import_module("saas_core.jobs").enqueue_job(
                params.tenant, "provision", dedupe_key=f"provision:{params.tenant}")
        return CheckoutResult(url=params.success_url, session_ref=f"mock_cs_{params.tenant}",
                              payment_mocked=True,
                              raw={"payment_mocked": 1, "subscription": sub.get("name"), "status": "active"})

    def portal_link(self, customer_ref: str, return_url: str) -> str:
        return return_url

    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        rows = frappe.get_all("Subscription", filters={"gateway": "manual", "gateway_sub_id": sub_ref}, pluck="name")
        for n in rows:
            sub = frappe.get_doc("Subscription", n)
            if at_period_end:
                sub.cancel_at_period_end = 1
            else:
                sub.status = "canceled"
            sub.save(ignore_permissions=True)

    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        return {"id": f"manual_refund_{payment_id}", "status": "processed", "amount": amount_minor,
                "payment_id": payment_id}

    def update_subscription_plan(self, sub_ref: str, plan_id: str, *, at_cycle_end: bool = True) -> dict:
        return {}

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        return False  # the manual gateway has no webhooks

    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        return NormalizedEvent(event_id=str((payload or {}).get("id") or ""), gateway="manual", kind="ignored")


TEST_CARD_BANNER = "Test mode \u2014 use a test card, no real charge"


def payment_banner() -> str | None:
    """Banner for the effective default gateway: manual => mock text; stripe/razorpay test => test-card text;
    live (or no enabled account) => None."""
    try:
        default = get_setting("default_gateway")
        filters = {"enabled": 1}
        if default:
            filters["gateway"] = default
        rows = frappe.get_all("Gateway Account", filters=filters, fields=["gateway", "mode"],
                              order_by="creation asc", limit=1)
    except Exception:
        return None
    if not rows:
        return None
    gw, mode = rows[0].get("gateway"), rows[0].get("mode")
    if gw == "manual":
        return get_setting("payment_banner") or DEFAULT_BANNER
    return TEST_CARD_BANNER if mode == "test" else None
