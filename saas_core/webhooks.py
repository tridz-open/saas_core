"""Guest webhook receivers: verify signature, store raw Payment Event once, enqueue processing."""
from __future__ import annotations

import json

import frappe

from saas_core.gateways import registry

PROCESSOR = "saas_core.billing.processor.process_payment_event"


def _fail(status: int, exc, msg: str):
    if getattr(frappe.local, "response", None) is None:
        frappe.local.response = {}
    frappe.local.response["http_status_code"] = status
    raise exc(msg)


def ingest(gateway: str, headers: dict, raw_body: bytes, now=None) -> dict:
    try:
        gw = registry.get_gateway(gateway)
    except Exception:  # unknown gateway / no enabled account: generic answer, no configuration details
        gw = None
    if gw is None:
        _fail(400, frappe.ValidationError, "Invalid webhook request")
    if not gw.verify_webhook(headers, raw_body):
        _fail(401, frappe.AuthenticationError, "Invalid webhook signature")
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        _fail(400, frappe.ValidationError, "Invalid webhook payload")
    _capture_if_authorized(gw, payload)
    ev = gw.parse_event(payload, headers)
    if not ev.event_id:
        _fail(400, frappe.ValidationError, "Missing event id")
    doc, is_new = record_event(gateway, ev, raw_body)
    if not is_new:
        return {"status": "duplicate"}
    if doc.status == "ignored":
        return {"status": "ignored"}
    return {"status": "accepted"}


def _capture_if_authorized(gw, payload: dict) -> None:
    """payment.authorized / order.paid for an order payment still `authorized`: capture it (idempotent). On
    failure answer 502 so the gateway retries; nothing is recorded or provisioned meanwhile."""
    if payload.get("event") not in ("payment.authorized", "order.paid") or not hasattr(gw, "ensure_captured"):
        return
    pay = (((payload.get("payload") or {}).get("payment") or {}).get("entity")) or {}
    if not pay.get("id") or not pay.get("order_id") or pay.get("status") not in ("authorized", None):
        return
    if payload.get("event") == "order.paid" and pay.get("status") != "authorized":
        return
    try:
        gw.ensure_captured(pay["id"])
    except Exception as e:  # noqa: BLE001
        frappe.log_error(f"saas_core capture failed for {pay.get('id')}: {str(e)[:300]}", "saas_core capture")
        _fail(502, frappe.ValidationError, "Payment capture failed")


def record_event(gateway: str, event, raw_body: bytes | str | None = None):
    """Idempotently store a Payment Event keyed `gateway:event_id`; enqueue processing only on first insert.

    Returns (doc, is_new). Safe to call from both the webhook and verify_payment paths.
    """
    key = f"{gateway}:{event.event_id}"
    if event.kind == "ignored":
        # ignored events (e.g. payment.authorized) must never claim the payment-id key: a later
        # actionable event for the same payment (callback, payment.captured) would be deduped away
        key = f"{key}:ignored:{(getattr(event, 'raw', None) or {}).get('event', 'unknown')}"
    existing = frappe.db.exists("Payment Event", key)
    if existing:
        return frappe.get_doc("Payment Event", key), False
    if raw_body is None:
        raw_body = json.dumps(getattr(event, "raw", None) or {})
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", "replace")
    ignored = event.kind == "ignored"
    doc = frappe.get_doc({
        "doctype": "Payment Event", "name": key, "event_key": key, "gateway": gateway, "event_id": event.event_id,
        "kind": event.kind, "status": "ignored" if ignored else "received", "raw_payload": raw_body,
        "subscription_ref": event.subscription_ref, "tenant_ref": event.tenant_ref, "attempts": 0,
    })
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return frappe.get_doc("Payment Event", key), False
    if not ignored:
        frappe.enqueue(PROCESSOR, payment_event=doc.name, enqueue_after_commit=True)
    return doc, True


def _receive(gateway: str) -> dict:
    from saas_core.api.v1._common import client_ip, rate_limit
    rate_limit(f"webhook:{gateway}:{client_ip()}", 300, 60)
    req = frappe.request
    return ingest(gateway, dict(req.headers), req.get_data())


@frappe.whitelist(allow_guest=True)
def razorpay():
    return _receive("razorpay")


@frappe.whitelist(allow_guest=True)
def stripe():
    return _receive("stripe")
