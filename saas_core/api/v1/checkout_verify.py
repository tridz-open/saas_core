"""checkout.verify_payment: server-verified client callback (e.g. Razorpay Checkout handler).

Goes through the same Payment Event path as webhooks (dedupe key `gateway:event_id`, same processor job).
"""
from __future__ import annotations

import json

import frappe

from ._common import ApiError, endpoint, gateway_for, json_body, require_fields, require_login, tenant_access

GATEWAYS = ("razorpay", "stripe", "manual")


@endpoint()
def verify_payment(gateway=None, payload=None):
    body = json_body(locals())
    user = require_login()
    require_fields(body, "gateway", "payload")
    pl = body["payload"]
    if body["gateway"] not in GATEWAYS:
        raise ApiError("validation_error", "Invalid request", 422, {"gateway": "unsupported gateway"})
    from saas_core.gateways.policy import resolve_gateway
    name = resolve_gateway(body["gateway"], user)  # P0-2: client gateway ignored unless default / System Manager
    if isinstance(pl, str):
        try:
            pl = json.loads(pl)
        except ValueError:
            pl = None
    if not isinstance(pl, dict):
        raise ApiError("validation_error", "Invalid request", 422, {"payload": "must be an object"})

    from saas_core.gateways.base import GatewayError
    try:
        gw = gateway_for(name)
    except frappe.ValidationError:
        raise ApiError("validation_error", "Payment gateway unavailable", 422)
    from saas_core.gateways.base import has_capability
    if not has_capability(gw, "client_callback"):
        raise ApiError("validation_error", "This payment gateway completes via webhook, not a client callback", 422)
    try:
        ev = gw.verify_client_callback(pl)
    except (GatewayError, NotImplementedError):
        raise ApiError("invalid_signature", "Invalid payment signature", 401)
    if ev is None or not ev.event_id:
        raise ApiError("invalid_signature", "Invalid payment signature", 401)

    # P0-1: tenant/plan/interval/billing mode come from the server-side Checkout Session record (or a gateway
    # API fetch of the notes we set), never from the client payload.
    from saas_core.billing.checkout_sessions import find_session, store_session
    refs = (ev.subscription_ref, ev.invoice_ref)
    sess = find_session(name, *refs)
    if sess is None and hasattr(gw, "fetch_session"):
        for ref in refs:
            if not ref:
                continue
            try:
                d = gw.fetch_session(ref) or {}
            except Exception:
                d = {}
            if d.get("tenant") and d.get("user") in (None, user):
                sess = store_session(name, ref, tenant=d["tenant"], owner_user=d.get("user") or user,
                                     plan=d.get("plan"), interval=d.get("interval"), currency=d.get("currency"),
                                     billing_mode=d.get("billing_mode"), amount_minor=d.get("amount_minor"))
                break
    if sess is not None:
        tenant = sess.get("tenant")
    elif name == "manual":  # test-mode only (registry refuses it next to a live account); no payment to bind
        tenant = ev.tenant_ref
    else:
        tenant = None
    if not tenant:
        raise ApiError("not_found", "Checkout session not found", 404)
    tenant_access(tenant, user, "write")
    ev.tenant_ref = tenant

    from saas_core.webhooks import record_event
    # P2-6: store only what the gateway signed (plus our paid flag), never the client's notes.
    doc, is_new = record_event(name, ev, json.dumps(ev.raw or {}, default=str))
    if not is_new:
        # The same key may already hold the gateway webhook for this payment (payment.captured -> payment.succeeded)
        # that is still unprocessed (e.g. it ran before the Subscription existed) or of another kind. Do not let that
        # swallow the paid callback: activate from the session now, idempotently.
        if doc.get("status") in ("processed", "ignored") and doc.get("kind") == ev.kind:
            return {"status": "duplicate"}
        from saas_core.billing import processor
        try:
            processor.process_verified_callback(ev, tenant)
            if doc.get("status") == "received":
                # The signed callback binds this payment id to its subscription, which the webhook payload lacks
                # (a subscription payment entity carries only invoice_id/order_id): resolve the waiting event now.
                processor.process_payment_event(doc.get("name"), subscription_ref_hint=ev.subscription_ref)
        except Exception:
            frappe.log_error(f"saas_core verify_payment direct processing failed for {doc.get('name')}")
        return {"status": "accepted"}
    return {"status": "ignored" if doc.get("status") == "ignored" else "accepted"}
