"""Day-based plan change. Quote arithmetic lives in proration.change_quote; this module applies it.

Flow
----
* preview(): read-only quote.
* apply(): downgrade -> refund the credit to the original payment and schedule the change; upgrade -> payment link
  for the difference (state awaiting_payment, nothing scheduled) unless trialing / nothing due, then schedule.
* on_link_paid(): the link settled -> schedule (idempotent).
* sweep_due(): flip Subscription.plan / Tenant.plan once plan_change_at has passed; expire unpaid links after 24h.

Stored values (UTC-naive datetimes, minor-unit ints):
  pending_plan, plan_change_state (awaiting_payment|scheduled), plan_change_at = effective instant (start of the next
  IST day; set already while awaiting payment), plan_change_link_id, plan_change_amount = amount due (upgrade, link
  amount) or credit (downgrade). The held amount after the change is recomputed from stored pieces on payment:
  prorate_down(paid_amount, days from plan_change_at to period_end, cycle_days) + plan_change_amount.
On scheduling, paid_amount := that new held amount and period_start := plan_change_at (segment start).
"""
from __future__ import annotations

import datetime as _dt

import frappe

from saas_core.billing import proration

AWAIT_EXPIRY = _dt.timedelta(hours=24)


class PlanChangeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _now(now=None) -> _dt.datetime:
    return proration.to_aware(now) if now is not None else proration.utc_now()


def _naive(now) -> _dt.datetime:
    return proration.to_naive_utc(now)


def price_row(plan: str, interval, currency, gateway):
    """Plan Price row for the subscription's interval/currency (gateway-specific row preferred), or None."""
    prices = frappe.get_doc("Plan", plan).get("prices") or []
    rows = [r for r in prices if r.get("interval") == (interval or "month")]
    if currency:
        rows = [r for r in rows if r.get("currency") == currency]
    if gateway:
        gw = [r for r in rows if not r.get("gateway") or r.get("gateway") == gateway]
        rows = gw or rows
    return rows[0] if rows else None


def _amount(row) -> int:
    try:
        return int(round(float(row.get("amount") or 0)))
    except (TypeError, ValueError):
        return 0


def _held(sub) -> int:
    if sub.get("paid_amount"):
        return int(sub.get("paid_amount"))
    rows = frappe.get_all("Invoice", filters={"subscription": sub.get("name"), "status": "paid"},
                          fields=["total_amount", "paid_at"], order_by="paid_at desc", limit=1)
    return int(rows[0].get("total_amount") or 0) if rows else 0


def _prices(sub, new_plan):
    if not new_plan or not frappe.db.exists("Plan", new_plan):
        raise PlanChangeError("unknown_plan", "Unknown plan")
    if sub.get("plan") == new_plan:
        raise PlanChangeError("same_plan", "Already on this plan")
    if not sub.get("period_end"):
        raise PlanChangeError("no_period_end", "Subscription has no billing period")
    iv, cur, gw = sub.get("interval"), sub.get("currency"), sub.get("gateway")
    new_row, old_row = price_row(new_plan, iv, cur, gw), price_row(sub.get("plan"), iv, cur, gw)
    if new_row is None or old_row is None:
        raise PlanChangeError("no_price", "No price for this plan and billing interval")
    return new_row, old_row


def _start(sub):
    return sub.get("period_start") or sub.get("period_end") - _dt.timedelta(days=30)


def _quote(sub, new_plan, now):
    new_row, old_row = _prices(sub, new_plan)
    q = proration.change_quote(_held(sub), _amount(new_row), _amount(old_row), _start(sub), sub.get("period_end"), now)
    return q, new_row, old_row


def _trialing(sub) -> bool:
    return sub.get("status") == "trialing"


def preview(sub, new_plan, now=None) -> dict:
    now = _now(now)
    q, new_row, _old = _quote(sub, new_plan, now)
    out = q.as_dict()
    out.update(tenant=sub.get("tenant"), from_plan=sub.get("plan"), to_plan=new_plan,
               currency=new_row.get("currency") or sub.get("currency"),
               payment_required=q.direction == "upgrade" and q.amount_due_minor > 0 and not _trialing(sub),
               renewal_date=q.cycle_end.isoformat(), new_cycle_price_minor=_amount(new_row),
               timezone=proration.BILLING_TZ_NAME)
    return out


def _flag(sub, reason: str, detail: str = "") -> None:
    frappe.log_error(f"plan change {sub.get('name')}: {reason} {detail}".strip(), "saas_core needs_attention")
    try:
        from saas_core import events
        events.publish("billing.needs_attention", tenant=sub.get("tenant"),
                       data={"subscription": sub.get("name"), "reason": reason})
    except Exception:  # noqa: BLE001 - alerting must never block the change
        frappe.log_error("plan change needs_attention publish failed")


def _gateway(sub):
    from saas_core.gateways import registry
    return registry.get_gateway(sub.get("gateway") or "razorpay")


def _refund(sub, user, credit: int, currency, now=None) -> dict | None:
    """Refund the downgrade credit, split across the payments that funded the cycle (newest first). Reuses Refund
    docs already created for this change (retry safe); failed ones are retried by cancellation.sweep_due."""
    if credit <= 0:
        return None
    from saas_core.billing import refunds
    reason = f"Plan change to {sub.get('pending_plan') or ''}".strip()
    # A change scheduled for `eff` is requested during the IST day before it, and period_start already equals eff
    # by the time a retry runs, so the window opens one day before the segment start.
    seg = sub.get("period_start")
    if seg:
        seg = seg - _dt.timedelta(days=1)
    existing = [d for d in frappe.get_all("Refund", filters={"subscription": sub.get("name"), "kind": "plan_change",
                                                           "reason": reason})
                if not seg or not d.get("requested_at") or d.get("requested_at") >= seg]
    if existing:
        return refunds.summarize(existing)
    now_dt = _now(now)
    now = _naive(now_dt)
    base_pid = refunds.base_payment_id(sub)
    parts, left, unallocated = refunds.split(sub, credit, base_pid, _held(sub))
    if unallocated > 0:
        parts.append((base_pid, unallocated))
    docs = []
    gw = None
    for pid, amt in parts:
        doc = refunds.create_doc(sub, user=sub.get("owner_user") or user, kind="plan_change", amount=amt,
                                 currency=currency, payment_id=pid, reason=reason, now=now)
        if not pid:
            doc.status, doc.error = "failed", "payment id unknown"
            doc.save(ignore_permissions=True)
            _flag(sub, "refund_failed", doc.get("name"))
        else:
            gw = gw or _gateway(sub)
            if not refunds.issue(doc, sub, gw, now_dt):
                pass  # flagged inside issue(); the change still proceeds and the sweep retries the refund
        docs.append(doc)
    refunds.save_extras(sub, left)
    return refunds.summarize(docs)


def _held_after(sub) -> int:
    cyc = proration.cycle_of(_start(sub), sub.get("period_end"))
    eff = proration.billing_date(sub.get("plan_change_at"))
    rem = max(0, (cyc.end - eff).days)
    return proration.prorate_down(_held(sub), rem, cyc.days) + int(sub.get("plan_change_amount") or 0)


def _schedule(sub, new_plan, *, new_held: int, amount: int, effective_at, user=None) -> dict:
    old = sub.get("plan")
    eff = _naive(effective_at)
    sub.pending_plan, sub.plan_change_state, sub.plan_change_at = new_plan, "scheduled", eff
    sub.plan_change_amount, sub.paid_amount, sub.period_start = amount, int(new_held), eff
    sub.save(ignore_permissions=True)  # plan_change_link_id kept so a replayed link event is recognised
    row = None
    try:
        row = price_row(new_plan, sub.get("interval"), sub.get("currency"), sub.get("gateway"))
    except Exception:  # noqa: BLE001
        pass
    if sub.get("gateway_sub_id") and row is not None and row.get("gateway_price_id"):
        try:
            gw = _gateway(sub)
            from saas_core.gateways.base import require_capability
            require_capability(gw, "plan_update", "provider subscription plan cannot be changed automatically")
            gw.update_subscription_plan(sub.get("gateway_sub_id"), row.get("gateway_price_id"),
                                                   at_cycle_end=True)
        except Exception as e:  # noqa: BLE001 - local schedule stands; ops re-syncs the gateway plan
            frappe.log_error(f"update_subscription_plan failed: {type(e).__name__} "
                             f"code={str(getattr(e, 'code', '') or '')[:60]} "
                             f"description={str(getattr(e, 'description', '') or '')[:200]}", "saas_core plan change")
            _flag(sub, "gateway_plan_update_failed")
    from saas_core import audit, jobs
    job = jobs.enqueue_job(sub.get("tenant"), "change_plan",
                           args={"plan": new_plan, "from": old, "effective_at": eff.isoformat()},
                           dedupe_key=f"change_plan:{sub.get('tenant')}:{new_plan}")
    audit.log("subscription.plan_change", actor=user, target_doctype="Subscription", target=sub.get("name"),
              details={"from": old, "to": new_plan, "effective_at": eff.isoformat(), "amount": amount})
    return {"scheduled": True, "job": job}


def apply(sub, new_plan, user, now=None) -> dict:
    now = _now(now)
    if sub.get("plan_change_state") == "scheduled":
        if sub.get("pending_plan") == new_plan:
            return {"scheduled": True, "payment_required": False, "job": None, "already": True,
                    "proration": preview_pending(sub, new_plan)}
        raise PlanChangeError("change_pending", "A plan change is already scheduled")
    q, new_row, _old = _quote(sub, new_plan, now)
    currency = new_row.get("currency") or sub.get("currency")
    prop = preview(sub, new_plan, now)
    if q.direction == "upgrade" and q.amount_due_minor > 0 and not _trialing(sub) and sub.get("gateway"):
        email = frappe.db.get_value("User", sub.get("owner_user"), "email") or sub.get("owner_user")
        stamp = int(now.timestamp())
        link = _gateway(sub).create_payment_link(
            amount=q.amount_due_minor, currency=currency,
            description=f"Upgrade to {new_plan}: {q.days_remaining} remaining days", email=email,
            notes={"tenant": sub.get("tenant"), "subscription": sub.get("name"), "kind": "plan_change"},
            reference_id=f"{sub.get('name')}-chg-{stamp}")
        sub.pending_plan, sub.plan_change_state = new_plan, "awaiting_payment"
        sub.plan_change_at, sub.plan_change_link_id = _naive(q.effective_at), link.get("id")
        sub.plan_change_amount = q.amount_due_minor
        sub.save(ignore_permissions=True)
        return {"scheduled": False, "payment_required": True, "payment_url": link.get("short_url"),
                "payment_link_id": link.get("id"), "proration": prop, "job": None}
    sub.pending_plan = new_plan
    refund = _refund(sub, user, q.credit_minor, currency, now) if q.direction == "downgrade" else None
    out = _schedule(sub, new_plan, new_held=q.new_held_minor, amount=q.credit_minor or q.amount_due_minor,
                    effective_at=q.effective_at, user=user)
    out.update(payment_required=False, proration=prop, refund=refund)
    return out


def preview_pending(sub, new_plan) -> dict:
    return {"to_plan": new_plan, "effective_at": sub.get("plan_change_at").isoformat() if sub.get("plan_change_at") else None}


def _refund_late_payment(sub, payment_id, amount, link_id) -> bool:
    """A payment-link payment that can no longer be applied (link expired / superseded): give it back in full."""
    from saas_core.billing import refunds
    if not payment_id or amount <= 0:
        _flag(sub, "late_link_payment_unrefundable", str(link_id))
        return False
    if frappe.get_all("Refund", filters={"gateway_payment_id": payment_id}, pluck="name"):
        return False  # replay
    now = _naive(proration.utc_now())
    doc = refunds.create_doc(sub, user=sub.get("owner_user"), kind="plan_change", amount=amount,
                             currency=sub.get("currency"), payment_id=payment_id,
                             reason="Upgrade payment received after the link expired", now=now)
    refunds.issue(doc, sub, _gateway(sub))
    sub.save(ignore_permissions=True)
    _flag(sub, "late_link_payment_refunded", doc.get("name"))
    return True


def on_link_paid(sub, now=None, payment_id=None, link_id=None, amount=None) -> bool:
    """The upgrade payment link settled. Schedules the change when still awaiting payment (replay safe). A payment
    for an expired or superseded link is refunded in full instead. True when the change was scheduled."""
    state = sub.get("plan_change_state")
    mine = not link_id or not sub.get("plan_change_link_id") or link_id == sub.get("plan_change_link_id")
    if state == "awaiting_payment" and sub.get("pending_plan") and mine:
        from saas_core.billing import refunds
        refunds.add_extra(sub, payment_id, int(sub.get("plan_change_amount") or 0))
        _schedule(sub, sub.get("pending_plan"), new_held=_held_after(sub),
                  amount=int(sub.get("plan_change_amount") or 0), effective_at=sub.get("plan_change_at"))
        return True
    if state == "scheduled" and mine:
        return False  # replay of the payment that scheduled it
    if payment_id and link_id:
        _refund_late_payment(sub, payment_id, int(amount or 0), link_id)
    return False


def sweep_due(now=None) -> dict:
    now = _naive(_now(now))
    out = {"switched": 0, "expired": 0, "errors": 0}
    for name in frappe.get_all("Subscription", filters={"plan_change_state": "scheduled"}, pluck="name"):
        try:
            sub = frappe.get_doc("Subscription", name)
            if not sub.get("plan_change_at") or sub.get("plan_change_at") > now:
                continue
            new = sub.get("pending_plan")
            sub.plan, sub.pending_plan, sub.plan_change_state = new, "", ""
            sub.plan_change_at, sub.plan_change_link_id, sub.plan_change_amount = None, "", 0
            sub.save(ignore_permissions=True)
            if sub.get("tenant") and frappe.db.exists("Tenant", sub.get("tenant")):
                tenant = frappe.get_doc("Tenant", sub.get("tenant"))
                tenant.plan = new
                tenant.save(ignore_permissions=True)
            out["switched"] += 1
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            frappe.log_error(f"plan change sweep failed for {name}")
    for name in frappe.get_all("Subscription", filters={"plan_change_state": "awaiting_payment"}, pluck="name"):
        try:
            sub = frappe.get_doc("Subscription", name)
            started = sub.get("modified")  # last save = when the link was issued (nothing else touches it meanwhile)
            at = sub.get("plan_change_at")
            stale = (started is not None and now - started >= AWAIT_EXPIRY) or (at is not None and at <= now)
            if not stale:
                continue
            sub.pending_plan, sub.plan_change_state = "", ""
            sub.plan_change_at, sub.plan_change_amount = None, 0  # link id kept: a late payment is still recognised
            sub.save(ignore_permissions=True)
            out["expired"] += 1
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            frappe.log_error(f"plan change expiry failed for {name}")
    return out
