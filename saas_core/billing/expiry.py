"""Daily sweep for subscriptions that outlived their period without a webhook/invoice driving them."""
from __future__ import annotations

import datetime as _dt

import frappe

INVOICE_GRACE = _dt.timedelta(days=1)
GATEWAY_STALE = _dt.timedelta(days=3)


def _now(now=None):
    """Naive UTC, like period_start/period_end/issued_at (never site-local now_datetime())."""
    from saas_core.billing import proration
    return proration.naive_utc_now(now)


def _cancel(sub, now):
    sub.status = "canceled"
    sub.save(ignore_permissions=True)
    t = sub.get("tenant")
    if t:
        from saas_core import jobs
        jobs.enqueue_job(t, "suspend", args={"reason": "canceled"}, dedupe_key=f"suspend:canceled:{t}")
        tenant = frappe.get_doc("Tenant", t)
        tenant.suspend_reason = "canceled"
        tenant.save(ignore_permissions=True)


def _flag(sub, now):
    frappe.log_error(f"subscription {sub.get('name')} past period_end with no charge event", "saas_core needs_attention")
    from saas_core import events
    events.publish("billing.needs_attention", tenant=sub.get("tenant"),
                   data={"subscription": sub.get("name"), "reason": "no_charge_event"})


def sweep_expired(now=None) -> dict:
    """Returns counts: canceled, dunned, flagged."""
    from frappe.utils import get_datetime
    from saas_core.billing import dunning
    from saas_core.experimental import enabled
    out = {"canceled": 0, "dunned": 0, "flagged": 0}
    if not enabled("expiry_sweep"):  # EXPERIMENTAL
        return out
    now = _now(now)
    names = frappe.get_all("Subscription", filters={"status": ["in", ["active", "past_due"]]}, pluck="name")
    for n in names:
        try:
            sub = frappe.get_doc("Subscription", n)
            if not sub.get("period_end"):
                continue
            end = get_datetime(sub.get("period_end"))
            if end > now:
                continue
            if sub.get("billing_mode") == "invoice":
                if sub.get("cancel_at_period_end"):
                    _cancel(sub, now)
                    out["canceled"] += 1
                    continue
                for iname in frappe.get_all("Invoice", filters={"subscription": n, "status": "open"}, pluck="name")[:1]:
                    inv = frappe.get_doc("Invoice", iname)
                    if now - get_datetime(inv.get("issued_at") or now) >= INVOICE_GRACE and sub.get("status") != "past_due":
                        if dunning.on_payment_failed(sub, now):
                            out["dunned"] += 1
            elif sub.get("status") == "active" and now - end > GATEWAY_STALE:
                last = sub.get("last_event_at")
                if not last or get_datetime(last) <= end:
                    _flag(sub, now)
                    out["flagged"] += 1
        except Exception:
            frappe.log_error(f"saas_core sweep_expired failed for {n}")
    return out
