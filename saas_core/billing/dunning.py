"""Dunning: staged escalation after failed payments; suspends tenant, resumes on payment."""
from __future__ import annotations

import datetime as _dt

import frappe

DUNNING_SCHEDULE = [1, 3, 7, 14]  # days after first failure -> stage 1..4
SUSPEND_STAGE = 3
FINAL_STAGE = 4


def _now(now=None):
    """Naive UTC, like period_start/period_end/issued_at (never site-local now_datetime())."""
    from saas_core.billing import proration
    return proration.naive_utc_now(now)


def _dt_of(v):
    from frappe.utils import get_datetime
    return get_datetime(v)


def _stage_for(first_failure, now) -> int:
    days = (now - _dt_of(first_failure)).total_seconds() / 86400.0
    return sum(1 for d in DUNNING_SCHEDULE if days >= d)


def _set_schedule(sub, stage):
    """next_retry_at = first_failure_at + next schedule day after `stage` (None once final); suspend_date = first_failure_at + suspend-stage day."""
    ff = sub.get("first_failure_at")
    if not ff:
        sub.next_retry_at = sub.suspend_date = None
        return
    ff = _dt_of(ff)
    sub.next_retry_at = ff + _dt.timedelta(days=DUNNING_SCHEDULE[stage]) if stage < len(DUNNING_SCHEDULE) else None
    sub.suspend_date = ff + _dt.timedelta(days=DUNNING_SCHEDULE[SUSPEND_STAGE - 1])


def _sub(subscription):
    return frappe.get_doc("Subscription", subscription) if isinstance(subscription, str) else subscription


def _suspend(sub, stage_now=True):
    from saas_core import jobs
    t = sub.get("tenant")
    if not t:
        return
    tenant = frappe.get_doc("Tenant", t)
    if tenant.get("suspend_reason") == "dunning":
        return
    jobs.enqueue_job(t, "suspend", args={"reason": "dunning"}, dedupe_key=f"suspend:dunning:{t}")
    tenant.suspend_reason = "dunning"
    tenant.save(ignore_permissions=True)
    _publish("billing.tenant_suspended", sub)


def _publish(type, sub, **data):
    from saas_core import events
    events.publish(type, tenant=sub.get("tenant"), data={"subscription": sub.get("name"), **data})


def _notify(sub, stage):
    frappe.log_error(f"dunning stage {stage} for subscription {sub.get('name')}", "saas_core dunning")
    _publish("billing.dunning_notice", sub, stage=stage)


INACTIVE_STATUSES = ("canceled", "expired")


def _advance(sub, now):
    from saas_core.experimental import enabled
    if not enabled("dunning"):  # EXPERIMENTAL: staged dunning/suspend is not live-verified
        return
    if sub.get("status") in INACTIVE_STATUSES:
        return
    ff = sub.get("first_failure_at")
    if not ff or sub.get("status") != "past_due":
        return
    new = _stage_for(ff, now)
    old = int(sub.get("dunning_stage") or 0)
    if new > old:
        sub.dunning_stage = new
        _set_schedule(sub, new)
        sub.save(ignore_permissions=True)
        frappe.enqueue("saas_core.notifications.billing_mail.send_dunning", subscription=sub.get("name"), attempt=new, enqueue_after_commit=True)
        if new >= SUSPEND_STAGE:
            _suspend(sub)
        if new >= FINAL_STAGE and old < FINAL_STAGE:
            _notify(sub, new)
    elif new >= SUSPEND_STAGE:
        _suspend(sub)  # idempotent


def on_payment_failed(subscription, now=None) -> bool:
    """Start/advance dunning. Returns False (no-op) for canceled/expired subscriptions."""
    sub = _sub(subscription)
    now = _now(now)
    if sub.get("status") in INACTIVE_STATUSES:
        return False
    sub.status = "past_due"
    if not sub.get("first_failure_at"):
        sub.first_failure_at = now
    _set_schedule(sub, int(sub.get("dunning_stage") or 0))
    sub.save(ignore_permissions=True)
    _publish("billing.payment_failed", sub)
    _advance(sub, now)
    return True


def on_payment_succeeded(subscription) -> None:
    sub = _sub(subscription)
    sub.dunning_stage = 0
    sub.first_failure_at = None
    sub.next_retry_at = sub.suspend_date = None
    sub.save(ignore_permissions=True)
    t = sub.get("tenant")
    if t and frappe.db.exists("Tenant", t):
        tenant = frappe.get_doc("Tenant", t)
        if tenant.get("suspend_reason") == "dunning":
            from saas_core import jobs
            jobs.enqueue_job(t, "resume", dedupe_key=f"resume:dunning:{t}:{sub.get('name')}")
            tenant.suspend_reason = None
            tenant.save(ignore_permissions=True)
            _publish("billing.tenant_resumed", sub)


def run_daily(now=None) -> int:
    """Advance dunning stages for all past_due subscriptions. Returns count processed."""
    from saas_core.experimental import enabled
    if not enabled("dunning"):
        return 0
    now = _now(now)
    names = frappe.get_all("Subscription", filters={"status": "past_due"}, pluck="name")
    for n in names:
        _advance(frappe.get_doc("Subscription", n), now)
    return len(names)
