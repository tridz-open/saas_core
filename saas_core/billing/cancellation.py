"""Day-based cancellation: quote, cancel (gateway first, then refund), day-end finalisation, refund retries.

The customer's site stays active to the end of the current IST day (`cancel_effective_at`); unused prepaid days
are refunded to the original payment method. All stored datetimes are UTC-naive.
"""
from __future__ import annotations

import calendar
import datetime as _dt

import frappe

from saas_core.billing import proration, refunds

REFUND_TO = "original payment method"
MAX_REFUND_ATTEMPTS = refunds.MAX_REFUND_ATTEMPTS


class CancelError(Exception):
    """cancel() refused or the gateway call failed. `.code` is already_canceling | plan_change_pending |
    gateway_error."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _dt_of(v):
    from frappe.utils import get_datetime
    return get_datetime(v) if v else None


def _naive_now(now=None) -> _dt.datetime:
    return proration.to_naive_utc(now or proration.utc_now())


def _iso(v) -> str | None:
    d = _dt_of(v)
    return proration.to_aware(d).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _minus_cycle(end: _dt.datetime, interval) -> _dt.datetime:
    months = 12 if interval == "year" else 1
    m = end.month - months
    y = end.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return end.replace(year=y, month=m, day=min(end.day, calendar.monthrange(y, m)[1]))


def _latest_paid_invoice(sub):
    rows = frappe.get_all("Invoice", filters={"subscription": sub.get("name"), "status": "paid"},
                          order_by="creation desc", limit=1)
    if not rows:
        rows = frappe.get_all("Invoice", filters={"tenant": sub.get("tenant"), "status": "paid"},
                              order_by="creation desc", limit=1)
    return rows[0] if rows else None


def _held(sub) -> dict:
    """period_start, period_end, paid, currency, invoice; legacy fallbacks when new fields are empty."""
    end = _dt_of(sub.get("period_end"))
    start = _dt_of(sub.get("period_start"))
    if end and not start:
        start = _minus_cycle(end, sub.get("interval"))
    inv = None
    paid = sub.get("paid_amount")
    if sub.get("status") == "trialing":
        paid = 0
    elif paid in (None, "") or (not int(paid) and not sub.get("gateway_payment_id")):
        # legacy: paid before paid_amount/gateway_payment_id were recorded (Int default 0, not None)
        inv = _latest_paid_invoice(sub)
        paid = int((inv or {}).get("total_amount") or 0)
    else:
        paid = int(paid)
    return {"start": start, "end": end, "paid": max(0, paid), "invoice": inv,
            "currency": sub.get("currency") or (inv or {}).get("currency") or ""}


def _payment_id(sub, held, gateway=None):
    """Payment that funded the cycle, or None when it cannot be determined."""
    if sub.get("gateway_payment_id"):
        return sub.get("gateway_payment_id")
    inv = held["invoice"] or _latest_paid_invoice(sub)
    if not inv:
        return None
    if inv.get("gateway_payment_id"):
        return inv.get("gateway_payment_id")
    gid = inv.get("gateway_invoice_id")
    if not gid:
        return None
    try:
        gw = gateway or _gateway(sub)
        from saas_core.gateways.base import has_capability
        if not has_capability(gw, "invoice_lookup"):
            return None  # cannot resolve: caller flags refund_payment_unknown
        return gw.invoice_payment_id(gid) or None
    except Exception:  # noqa: BLE001
        frappe.log_error(f"saas_core cancel: payment lookup failed for {sub.get('name')}", "saas_core cancel")
        return None


def _gateway(sub):
    from saas_core.gateways.registry import get_gateway
    return get_gateway(sub.get("gateway"))


def _cancel_refunds(sub) -> list:
    return frappe.get_all("Refund", filters={"subscription": sub.get("name"), "kind": "cancel"},
                          order_by="creation asc")


def quote(sub, now=None) -> dict:
    from saas_core.notifications import templating
    now = now or proration.utc_now()
    held = _held(sub)
    blocked = None
    if sub.get("status") == "canceled" or sub.get("cancel_effective_at"):
        blocked = "already_canceling"
    elif sub.get("plan_change_state") == "scheduled":
        blocked = "plan_change_pending"
    elif sub.get("plan_change_state") == "awaiting_payment":
        blocked = "plan_change_pending_payment"
    base = {"tenant": sub.get("tenant"), "subscription": sub.get("name"), "plan": sub.get("plan"),
            "plan_label": templating.plan_label(sub.get("plan")), "currency": held["currency"],
            "can_cancel": blocked is None, "blocked_reason": blocked,
            "refund_to": REFUND_TO, "renewal_date": proration.billing_date(held["end"]).isoformat() if held["end"] else None}
    if held["end"] is None:
        stops = proration.end_of_billing_day(now)
        base.update({"cycle_start": None, "cycle_end": None, "cycle_days": 0, "days_used": 0, "days_unused": 0,
                     "paid_minor": held["paid"], "charge_minor": held["paid"], "refund_minor": 0,
                     "stops_at": stops.strftime("%Y-%m-%dT%H:%M:%SZ"), "timezone": proration.BILLING_TZ_NAME})
        if sub.get("cancel_effective_at"):
            base["stops_at"] = _iso(sub.get("cancel_effective_at"))
        return base
    q = {**base, **proration.cancel_quote(held["paid"], held["start"], held["end"], now).as_dict()}
    if sub.get("cancel_effective_at"):  # already canceling: report what was actually decided, not a fresh quote
        rows = _cancel_refunds(sub)
        refund = sum(int(r.get("amount") or 0) for r in rows)
        q["stops_at"] = _iso(sub.get("cancel_effective_at"))
        q["refund_minor"] = refund
        q["charge_minor"] = max(0, held["paid"] - refund)
        if rows and rows[0].get("days_used") is not None:
            q["days_used"] = int(rows[0].get("days_used") or 0)
            q["days_unused"] = int(rows[0].get("days_unused") or 0)
    return q


def _flag(sub, why):
    refunds.flag(sub, why)


def _issue_refund(doc, sub, gateway, now):
    return refunds.issue(doc, sub, gateway, now)


def _attempts(doc) -> int:
    return refunds.attempts(doc)


def _refund_doc(sub, user, held, q, reason, payment_id, now):
    doc = refunds.create_doc(sub, user=user, kind="cancel", amount=q["refund_minor"], currency=held["currency"],
                             payment_id=payment_id, reason=reason, invoice=(held["invoice"] or {}).get("name"),
                             days_used=q["days_used"], days_unused=q["days_unused"], now=_naive_now(now))
    frappe.db.commit()  # the record must survive anything that fails after the gateway call
    return doc


_UNDO = ("canceled_at", "cancel_effective_at", "cancel_reason", "cancel_at_period_end")


def _already_cancelled(e) -> bool:
    msg = str(e).lower()
    return "already" in msg and "cancel" in msg


def _lock(sub) -> None:
    """Row lock on the Subscription for the rest of the transaction (released by the next commit)."""
    frappe.db.sql("select name from `tabSubscription` where name = %s for update", (sub.get("name"),))
    reload = getattr(sub, "reload", None)
    if callable(reload):
        reload()


def cancel(sub, user, reason=None, now=None) -> dict:
    now = now or proration.utc_now()
    _lock(sub)
    q = quote(sub, now)
    if not q["can_cancel"]:
        raise CancelError(q["blocked_reason"])
    if frappe.db.exists("Refund", {"subscription": sub.get("name"), "kind": "cancel"}):
        raise CancelError("already_canceling")
    gateway = _gateway(sub) if (sub.get("gateway_sub_id") or q["refund_minor"] > 0) else None
    held = _held(sub)
    stops_at = _dt_of(q["stops_at"])
    # persist the decision first so a retry or a second request sees it, whatever happens at the gateway
    prev = {f: sub.get(f) for f in _UNDO}
    sub.canceled_at = _naive_now(now)
    sub.cancel_effective_at = stops_at.replace(tzinfo=None)
    sub.cancel_reason = reason
    sub.cancel_at_period_end = 1
    sub.save(ignore_permissions=True)
    frappe.db.commit()
    if sub.get("gateway_sub_id"):
        try:
            gateway.cancel_subscription(sub.get("gateway_sub_id"), False)
        except Exception as e:  # noqa: BLE001
            if not _already_cancelled(e):
                frappe.log_error(f"saas_core cancel gateway error for {sub.get('name')}", "saas_core cancel")
                for f, v in prev.items():
                    setattr(sub, f, v)
                sub.save(ignore_permissions=True)
                frappe.db.commit()
                raise CancelError("gateway_error", "Payment provider unavailable") from e
    docs = []
    if q["refund_minor"] > 0:
        pid = _payment_id(sub, held, gateway)
        parts, left, unallocated = refunds.split(sub, q["refund_minor"], pid, held["paid"])
        if unallocated > 0:
            parts.append((pid, unallocated))  # more than the payments can give back: surfaces as a failed row
        inv = (held["invoice"] or {}).get("name")
        for payment_id, amt in parts:
            qq = {**q, "refund_minor": amt}
            if payment_id:
                doc = _refund_doc(sub, user, held, qq, reason, payment_id, now)
                _issue_refund(doc, sub, gateway, now)
            else:
                _flag(sub, "refund_payment_unknown")
                doc = refunds.create_doc(sub, user=user, kind="cancel", amount=amt, currency=held["currency"],
                                         payment_id=None, reason=reason, invoice=inv, days_used=q["days_used"],
                                         days_unused=q["days_unused"], now=_naive_now(now), status="failed",
                                         error="payment id unknown")
            docs.append(doc)
        refunds.save_extras(sub, left)
    sub.save(ignore_permissions=True)
    refund = docs[0] if docs else None
    rinfo = refunds.summarize(docs)
    try:
        from saas_core import audit
        audit.log("subscription.cancel", actor=user, target_doctype="Subscription", target=sub.get("name"),
                  details={"flow": "day_based", "reason": reason, "refund_minor": q["refund_minor"],
                           "refund": refund.get("name") if refund is not None else None,
                           "refunds": [d.get("name") for d in docs],
                           "days_used": q["days_used"], "stops_at": q["stops_at"]})
    except ImportError:
        pass
    from saas_core import events
    events.publish("subscription.updated", tenant=sub.get("tenant"),
                   data={"subscription": sub.get("name"), "status": "canceling"})
    frappe.enqueue("saas_core.notifications.billing_mail.send_cancellation", subscription=sub.get("name"),
                   refund=refund.get("name") if refund is not None else None, enqueue_after_commit=True)
    return {**q, "can_cancel": False, "status": "canceling", "refund": rinfo}


def _finalise(sub):
    from saas_core.billing import expiry
    expiry._cancel(sub, None)


def sweep_due(now=None) -> dict:
    """Finalise cancels whose day has ended; retry failed refunds. Returns counts."""
    n = _naive_now(now)
    out = {"canceled": 0, "refunds_retried": 0, "refunds_failed": 0}
    for name in frappe.get_all("Subscription", filters={"status": ["in", ["active", "past_due", "trialing"]]},
                               pluck="name"):
        try:
            sub = frappe.get_doc("Subscription", name)
            eff = _dt_of(sub.get("cancel_effective_at"))
            if eff and eff <= n:
                _finalise(sub)
                out["canceled"] += 1
        except Exception:  # noqa: BLE001
            frappe.log_error(f"saas_core cancellation sweep failed for {name}")
    for rname in frappe.get_all("Refund", filters={"status": "failed"}, pluck="name"):
        try:
            doc = frappe.get_doc("Refund", rname)
            if not doc.get("gateway_payment_id") or _attempts(doc) >= MAX_REFUND_ATTEMPTS:
                continue
            sub = frappe.get_doc("Subscription", doc.get("subscription"))
            ok = _issue_refund(doc, sub, _gateway(sub), now)
            sub.save(ignore_permissions=True)
            out["refunds_retried" if ok else "refunds_failed"] += 1
        except Exception:  # noqa: BLE001
            frappe.log_error(f"saas_core refund retry failed for {rname}")
    return out
