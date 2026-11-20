"""Payment Event processor: applies normalized gateway events to Subscription/Invoice/Tenant. Idempotent."""
from __future__ import annotations

import frappe


class Retryable(ValueError):
    """Transient condition (e.g. the Subscription is not created yet because the browser callback is still in
    flight): the event stays `received` and is retried with backoff, never failed terminally."""


# seconds to wait before attempt N+1 after the Nth transient failure; the 6th failure -> needs_attention
RETRY_BACKOFF = (10, 30, 120, 600, 600)
MAX_ATTEMPTS = len(RETRY_BACKOFF) + 1
SWEEP_AFTER_SECONDS = 120


def _now(now=None):
    """Naive UTC, like period_start/period_end/issued_at (never site-local now_datetime())."""
    from saas_core.billing import proration
    return proration.naive_utc_now(now)


def _find_sub(ev, sess=None):
    """Match ONLY by the gateway subscription ref, or by the subscription recorded on the Checkout Session.
    No "latest subscription of the tenant" fallback: an unrelated payment carrying notes.tenant must never
    touch (or reset dunning on) another subscription."""
    ref = ev.get("subscription_ref")
    if ref:
        rows = frappe.get_all("Subscription", filters={"gateway": ev.get("gateway"), "gateway_sub_id": ref}, pluck="name")
        if rows:
            return frappe.get_doc("Subscription", rows[0])
    if sess is not None and sess.get("subscription") and frappe.db.exists("Subscription", sess.get("subscription")):
        return frappe.get_doc("Subscription", sess.get("subscription"))
    return None


def _session(ev, p):
    """Checkout Session record for this event (by signed subscription/order id), or None.
    Gateway webhooks carry the same ids on the payment entity (subscription_id / order_id / invoice_id)."""
    from saas_core.billing.checkout_sessions import find_session
    pay, _, _ = _rz(p)
    return find_session(ev.get("gateway"), ev.get("subscription_ref"), p.get("razorpay_subscription_id"),
                        p.get("razorpay_order_id"), pay.get("subscription_id"), pay.get("order_id"),
                        ev.get("invoice_ref"), pay.get("invoice_id"))


_VIEW_FIELDS = ("name", "gateway", "kind", "subscription_ref", "tenant_ref", "invoice_ref", "amount", "currency",
                "period_end", "reason", "event_id")


def _view(ev) -> dict:
    """Mutable, in-memory copy of the event fields handlers read. Payment Event is immutable on those fields, so
    resolved refs (subscription from the Checkout Session) live here and are never written back."""
    return {k: ev.get(k) for k in _VIEW_FIELDS}


def _gateway_subscription_for_payment(ev, p):
    """Last resort when no local Checkout Session matches the payment's own refs: ask the gateway which
    subscription the payment belongs to (payment -> invoice -> subscription). None on any failure."""
    pay, _, _ = _rz(p)
    pid = pay.get("id") or p.get("razorpay_payment_id")
    if not pid:
        return None
    try:
        from saas_core.gateways import registry
        gw = registry.get_gateway(ev.get("gateway"))
        fn = getattr(gw, "payment_subscription_id", None)
        return fn(pid) if callable(fn) else None
    except Exception:
        return None


def _resolve_payment_refs(view, p):
    """Fill view['subscription_ref'] for a payment event from the server-side Checkout Session (never guessing by
    tenant). Returns the session or None."""
    from saas_core.billing.checkout_sessions import find_session
    sess = _session(view, p)
    if sess is None and not view.get("subscription_ref"):
        ref = _gateway_subscription_for_payment(view, p)
        if ref:
            view["subscription_ref"] = ref
            sess = find_session(view.get("gateway"), ref)
    if sess is not None and not view.get("subscription_ref") and sess.get("billing_mode") != "invoice":
        view["subscription_ref"] = sess.get("session_ref")
    return sess


def _activate_from_session(view, p, now):
    """payment.captured arrived before the Subscription exists: create/activate it from the Checkout Session via
    the same idempotent path as a paid checkout.completed. Returns the Subscription or None (no session)."""
    sess = _session(view, p)
    if sess is None:
        return None
    pay, _, _ = _rz(p)
    p2 = dict(p, paid=True, payment_ref=pay.get("id"))
    ref = view.get("subscription_ref") or (sess.get("session_ref") if sess.get("billing_mode") != "invoice" else None)
    if ref:
        p2["razorpay_subscription_id"] = ref
    elif sess.get("billing_mode") == "invoice":
        p2["razorpay_order_id"] = sess.get("session_ref")
    v2 = dict(view, kind="checkout.completed", subscription_ref=ref or view.get("subscription_ref"),
              tenant_ref=sess.get("tenant"))
    _checkout_completed(v2, p2, now)
    sess = _session(v2, p2)
    return _find_sub(v2, sess)


def process_verified_callback(event, tenant, now=None) -> None:
    """Server-verified browser callback (paid checkout.completed) processed directly, so activation never depends on
    webhook ordering. Same idempotent path (and tenant lock) as the queued event. `event` is a NormalizedEvent."""
    from saas_core.audit import cache_lock
    now = _now(now)
    view = {"name": None, "gateway": event.gateway, "kind": "checkout.completed",
            "subscription_ref": event.subscription_ref, "tenant_ref": tenant, "invoice_ref": event.invoice_ref,
            "event_id": event.event_id}
    with cache_lock(f"saas_core:billing:{tenant}"):
        try:
            _checkout_completed(view, dict(event.raw or {}), now)
        except PaymentMismatch as e:  # not activated/provisioned; the queued Payment Event is flagged on its own run
            _flag_mismatch(e, tenant=tenant)


def _link_session(sess, sub, now):
    if sess is not None and sub is not None and sess.get("subscription") != sub.get("name"):
        sess.subscription = sub.get("name")
        sess.consumed_at = now
        sess.save(ignore_permissions=True)


def _payload(ev) -> dict:
    p = ev.get("raw_payload")
    try:
        p = frappe.parse_json(p) if p else {}
    except Exception:
        p = {}
    return p if isinstance(p, dict) else {}


_PROVISION_JOB_OPEN = ("pending", "dispatching", "submitted", "running", "stuck", "outcome_unknown", "needs_attention")


def _maybe_provision(tenant_name):
    """Enqueue the provision job at most once per tenant, even if the client callback and the
    webhook both arrive for the same payment (check-and-create under a lock)."""
    if not tenant_name or not frappe.db.exists("Tenant", tenant_name):
        return
    from saas_core.audit import cache_lock
    with cache_lock(f"saas_core:provision:{tenant_name}"):
        if frappe.db.get_value("Tenant", tenant_name, "status") != "pending":  # provisioning/active/...: skip
            return
        if frappe.db.get_value("Provision Job", {"tenant": tenant_name, "kind": "provision",
                                                 "state": ["in", list(_PROVISION_JOB_OPEN)]}, "name"):
            return
        from saas_core import jobs
        jobs.enqueue_job(tenant_name, "provision", dedupe_key=f"provision:{tenant_name}")


def _rz(p):
    """(payment, payment_link, subscription) entities of a Razorpay webhook payload ({} when absent).
    The Payment Event doctype keeps only refs, so amount/invoice details are read back from the raw payload."""
    inner = p.get("payload") if isinstance(p.get("payload"), dict) else {}

    def ent(k):
        e = (inner.get(k) or {}).get("entity") if isinstance(inner.get(k), dict) else None
        return e if isinstance(e, dict) else {}
    return ent("payment"), ent("payment_link"), ent("subscription")


class PaymentMismatch(Exception):
    """Captured payment's currency/amount disagrees with the checkout session / invoice. Never activate."""

    def __init__(self, details: dict):
        self.details = details
        super().__init__(f"payment mismatch: {details}")


def _captured(p):
    """(amount_minor, currency) of the gateway-captured payment: fetched at verify time (`captured_*`, set by the
    gateway adapter from an API read) or the signed webhook payment entity. None when absent."""
    pay, _, _ = _rz(p)
    amount = p.get("captured_amount") if p.get("captured_amount") is not None else pay.get("amount")
    currency = p.get("captured_currency") or pay.get("currency")
    return (int(amount) if amount is not None else None), (str(currency).upper() if currency else None)


def _check_payment(p, *, expected_amount, expected_currency, ref=None):
    """Raise PaymentMismatch unless the captured payment matches the expected (server-side) amount and currency.
    Expectations that are unknown (0 / empty) are not compared; a known expectation with no captured data to
    compare against is a failure (unverifiable)."""
    amount, currency = _captured(p)
    exp_amount = int(expected_amount or 0)
    exp_currency = str(expected_currency).upper() if expected_currency else None
    problems = []
    if exp_currency and currency != exp_currency:
        problems.append("currency")
    if exp_amount > 0 and amount != exp_amount:
        problems.append("amount")
    if problems:
        raise PaymentMismatch({"fields": problems, "expected_amount": exp_amount, "expected_currency": exp_currency,
                               "captured_amount": amount, "captured_currency": currency, "ref": ref,
                               "payment_id": p.get("razorpay_payment_id") or _rz(p)[0].get("id")})


def _flag_mismatch(err, ev_name=None, tenant=None):
    """Visible record: audit entry + error log (callers also park the Payment Event as needs_attention)."""
    frappe.log_error(f"saas_core payment mismatch (event {ev_name}, tenant {tenant}): {err.details}")
    try:
        from saas_core import audit
        audit.log("payment.mismatch", actor="system", target_doctype="Payment Event" if ev_name else "Tenant",
                  target=ev_name or tenant, details=dict(err.details, tenant=tenant))
    except Exception:
        frappe.log_error(f"saas_core payment mismatch: audit failed for {ev_name or tenant}")


def _is_paid(p) -> bool:
    """True for a server-verified client callback (gateway signature checked before the event was recorded;
    webhook payloads never carry these keys)."""
    return bool(p.get("paid")) or bool(p.get("razorpay_payment_id") and p.get("razorpay_signature"))


def _is_invoice_checkout(ev, p, sess) -> bool:
    """Paid one-time order checkout (subscription-creation fallback, or an order-mode account). Decided by the
    server-side session record, or structurally by a signed order id with no subscription id."""
    if not _is_paid(p):
        return False
    if sess is not None:
        return sess.get("billing_mode") == "invoice"
    return bool(p.get("razorpay_order_id")) and not (ev.get("subscription_ref") or p.get("razorpay_subscription_id"))


def _utc_now():
    from saas_core.billing import proration
    return proration.to_naive_utc(proration.utc_now())


def _cycle_start(rsub, previous_end=None):
    """UTC-naive start of the cycle a payment funds: the gateway's current_start, else the previous period_end
    (renewal), else now."""
    if rsub.get("current_start"):
        from saas_core.gateways.base import from_ts
        return from_ts(rsub.get("current_start"))
    return previous_end or _utc_now()


def _period_after(start, interval):
    from frappe.utils import add_to_date
    return add_to_date(start, years=1) if interval == "year" else add_to_date(start, months=1)


def _first_invoice_payment(ev, p, now, tenant, plan, sess):
    """Fallback path: paid first order -> invoice-mode Subscription (no gateway_sub_id) + paid Invoice + provision.
    Interval, currency and amount come from the Checkout Session record, never from the callback body."""
    tname = tenant.get("name")
    interval = (sess.get("interval") if sess is not None else None) or "month"
    currency = sess.get("currency") if sess is not None else None
    amount = int((sess.get("amount_minor") if sess is not None else 0) or 0)
    sub = _find_sub(ev, sess)
    if sub is not None and sub.get("billing_mode") == "invoice" and sub.get("status") == "active" and sub.get("last_event_at"):
        _maybe_provision(tname)  # replay / duplicate callback
        return
    if sub is None:
        sub = frappe.get_doc({"doctype": "Subscription", "tenant": tname, "owner_user": tenant.get("owner_user"),
                              "plan": plan, "gateway": ev.get("gateway"), "dunning_stage": 0,
                              "interval": interval, "currency": currency, "status": "active"})
        sub.insert(ignore_permissions=True)
    sub.billing_mode = "invoice"
    sub.gateway_sub_id = ""
    sub.status = "active"
    sub.period_end = _period_after(now, interval)
    sub.period_start = _utc_now()
    sub.paid_amount = amount
    sub.gateway_payment_id = p.get("razorpay_payment_id") or sub.get("gateway_payment_id")
    sub.last_event_at = now
    sub.save(ignore_permissions=True)
    _link_session(sess, sub, now)
    iid = p.get("razorpay_order_id") or ev.get("invoice_ref") or p.get("razorpay_payment_id")
    gw = ev.get("gateway")
    if iid and not frappe.get_all("Invoice", filters={"gateway": gw, "gateway_invoice_id": iid}, pluck="name"):
        inv = frappe.get_doc({"doctype": "Invoice", "tenant": tname, "owner_user": tenant.get("owner_user"),
                              "subscription": sub.get("name"), "gateway": gw, "gateway_invoice_id": iid,
                              "status": "paid", "total_amount": amount,
                              "currency": currency, "issued_at": now, "paid_at": now,
                              "period_start": now, "period_end": sub.get("period_end"),
                              "gateway_payment_id": p.get("razorpay_payment_id")})
        inv.insert(ignore_permissions=True)
        frappe.enqueue("saas_core.notifications.billing_mail.send_invoice_paid", invoice=inv.name, enqueue_after_commit=True)
    tenant.plan = plan
    tenant.save(ignore_permissions=True)
    _maybe_provision(tname)


def _checkout_completed(ev, p, now):
    sess = _session(ev, p)
    tname = sess.get("tenant") if sess is not None else ev.get("tenant_ref")
    if not tname or not frappe.db.exists("Tenant", tname):
        raise ValueError(f"unknown tenant {tname!r}")
    tenant = frappe.get_doc("Tenant", tname)
    plan = (sess.get("plan") if sess is not None else None) or tenant.get("plan")
    paid = _is_paid(p)
    if paid and sess is not None:
        _check_payment(p, expected_amount=sess.get("amount_minor"), expected_currency=sess.get("currency"),
                       ref=sess.get("session_ref"))
    if _is_invoice_checkout(ev, p, sess):
        return _first_invoice_payment(ev, p, now, tenant, plan, sess)
    interval = sess.get("interval") if sess is not None else None
    currency = sess.get("currency") if sess is not None else None
    _, _, rsub = _rz(p)
    trial = int(frappe.db.get_value("Plan", plan, "trial_days") or 0) if plan and frappe.db.exists("Plan", plan) else 0
    sub = _find_sub(ev, sess)
    if sub is not None and sub.get("status") in ("active", "trialing") and sub.get("last_event_at"):
        if paid:  # activated earlier by the webhook path, which never provisions; callback proves payment
            _maybe_provision(tname)
        return  # status guard: already activated by the other path (callback vs webhook)
    if sub is None:
        sub = frappe.get_doc({"doctype": "Subscription", "tenant": tname, "owner_user": tenant.get("owner_user"),
                              "plan": plan, "gateway": ev.get("gateway"), "dunning_stage": 0,
                              "interval": interval, "currency": currency})
        sub.status = "trialing" if trial else "active"
        sub.insert(ignore_permissions=True)
    sub.gateway_sub_id = ev.get("subscription_ref") or p.get("razorpay_subscription_id") or sub.get("gateway_sub_id")
    sub.gateway_customer_id = rsub.get("customer_id") or sub.get("gateway_customer_id")
    if trial:
        from datetime import timedelta
        sub.status = "trialing"
        sub.trial_end = now + timedelta(days=trial)
    elif sub.get("status") != "past_due":
        sub.status = "active"
    if rsub.get("current_end"):  # gateway-signed webhook payload only; never a client callback field
        from saas_core.gateways.base import from_ts
        sub.period_end = from_ts(rsub.get("current_end"))
    elif paid and not sub.get("period_end"):
        sub.period_end = _period_after(now, sub.get("interval") or interval or "month")
    if paid and not trial:  # verified callback = first charge funds a new cycle
        sub.period_start = _cycle_start(rsub)
        sub.paid_amount = int((sess.get("amount_minor") if sess is not None else 0) or sub.get("paid_amount") or 0)
        sub.gateway_payment_id = p.get("razorpay_payment_id") or sub.get("gateway_payment_id")
    elif rsub.get("current_start") and not trial and not sub.get("period_start"):
        sub.period_start = _cycle_start(rsub)
    sub.last_event_at = now
    sub.save(ignore_permissions=True)
    _link_session(sess, sub, now)
    tenant.plan = plan
    tenant.save(ignore_permissions=True)
    if trial or paid:  # trial: no payment yet, starts provisioning; paid: verified callback = first charge
        _maybe_provision(tname)


def _payment_succeeded(ev, p, now):
    pay, link, rsub = _rz(p)
    gw = ev.get("gateway")
    iid = ev.get("invoice_ref") or link.get("id") or pay.get("invoice_id")
    if iid:
        plan_subs = frappe.get_all("Subscription", filters={"plan_change_link_id": iid}, pluck="name")
        if not plan_subs and isinstance(link.get("notes"), dict) and link["notes"].get("kind") == "plan_change":
            nsub = link["notes"].get("subscription")  # link superseded by a newer upgrade: still not a renewal
            plan_subs = [nsub] if nsub and frappe.db.exists("Subscription", nsub) else []
        if plan_subs:  # upgrade-difference payment link: schedules the plan change (or refunds a late payment)
            from saas_core.billing import plan_change
            plan_change.on_link_paid(frappe.get_doc("Subscription", plan_subs[0]), now, payment_id=pay.get("id"),
                                     link_id=iid, amount=int(pay.get("amount") or link.get("amount_paid") or 0))
            return
    amount = int(ev.get("amount") or pay.get("amount") or link.get("amount_paid") or 0)
    currency = ev.get("currency") or pay.get("currency")
    period_end = ev.get("period_end")
    if not period_end and rsub.get("current_end"):
        from saas_core.gateways.base import from_ts
        period_end = from_ts(rsub.get("current_end"))
    inv = None
    if iid:
        rows = frappe.get_all("Invoice", filters={"gateway": gw, "gateway_invoice_id": iid}, pluck="name")
        if rows:
            inv = frappe.get_doc("Invoice", rows[0])
    if inv is not None and inv.get("subscription") and frappe.db.exists("Subscription", inv.get("subscription")):
        sub = frappe.get_doc("Subscription", inv.get("subscription"))  # payment-link renewals carry no sub ref
    else:
        sub = _find_sub(ev, _session(ev, p))
    if sub is None:
        # Razorpay sends payment.captured concurrently with the browser callback, so it can win the race. Resolve
        # the subscription from the server-side Checkout Session and create/activate it from there.
        sub = _activate_from_session(ev, p, now)
    if sub is None:
        if not iid:
            return  # never create an Invoice with an empty gateway invoice id (unattributable payment)
        raise Retryable("subscription not found")
    if not iid:
        _maybe_provision(sub.get("tenant"))
        return
    if inv is None or inv.get("status") != "paid":
        sess_ = _session(ev, p)
        open_inv = inv is not None
        _check_payment(p, expected_amount=(inv.get("total_amount") if open_inv and inv.get("status") == "open"
                                           else (sess_.get("amount_minor") if sess_ is not None else 0)),
                       expected_currency=((inv.get("currency") if open_inv else None)
                                          or (sess_.get("currency") if sess_ is not None else None)
                                          or sub.get("currency")), ref=iid)
    invoice_mode = sub.get("billing_mode") == "invoice"
    if invoice_mode and inv is None:
        return  # payment.captured / order.paid echo of a payment already recorded via callback or payment link
    if inv is None:
        inv = frappe.get_doc({"doctype": "Invoice", "tenant": sub.get("tenant"), "owner_user": sub.get("owner_user"),
                              "subscription": sub.get("name"), "gateway": gw, "gateway_invoice_id": iid,
                              "issued_at": now})
        inv.insert(ignore_permissions=True)
    already_paid = inv.get("status") == "paid"
    pay_id = pay.get("id") or p.get("razorpay_payment_id")
    renewal = not already_paid and any(n != inv.get("name") for n in frappe.get_all(
        "Invoice", filters={"subscription": sub.get("name"), "status": "paid"}, pluck="name"))
    previous_end = sub.get("period_end")
    inv.status = "paid"
    inv.total_amount = amount or int(inv.get("total_amount") or 0)
    inv.currency = currency or inv.get("currency")
    inv.paid_at = inv.get("paid_at") if already_paid and inv.get("paid_at") else now
    if period_end or not invoice_mode:
        inv.period_end = period_end
    if pay_id and not inv.get("gateway_payment_id"):
        inv.gateway_payment_id = pay_id
    inv.save(ignore_permissions=True)
    if already_paid:  # same payment already applied via the other path
        _maybe_provision(sub.get("tenant"))
        return
    sub.status = "active"
    if invoice_mode:
        sub.period_end = inv.get("period_end") or _period_after(sub.get("period_end") or now, sub.get("interval"))
    elif period_end:
        sub.period_end = period_end
    if renewal:  # a new cycle: replaces the previous cycle's held amount; a pending plan change applied by now
        sub.period_start = _cycle_start(rsub, previous_end)
        sub.paid_amount = amount or int(inv.get("total_amount") or 0)
        sub.gateway_payment_id = pay_id or sub.get("gateway_payment_id")
        sub.pending_plan = ""
        sub.plan_change_state = ""
        sub.plan_change_at = None
        sub.plan_change_link_id = ""
        sub.plan_change_amount = 0
        sub.extra_payments = ""
    else:  # first payment: the client callback may have recorded these already; only fill what is missing
        if not sub.get("period_start"):
            sub.period_start = _cycle_start(rsub, previous_end if invoice_mode else None)
        if not sub.get("paid_amount"):
            sub.paid_amount = amount or int(inv.get("total_amount") or 0)
        if not sub.get("gateway_payment_id") and pay_id:
            sub.gateway_payment_id = pay_id
    sub.last_event_at = now
    sub.save(ignore_permissions=True)
    from saas_core.billing import dunning
    dunning.on_payment_succeeded(sub)
    frappe.enqueue("saas_core.notifications.billing_mail.send_renewal_receipt" if renewal
                   else "saas_core.notifications.billing_mail.send_invoice_paid",
                   invoice=inv.name, enqueue_after_commit=True)
    _maybe_provision(sub.get("tenant"))


def _payment_failed(ev, p, now):
    sub = _find_sub(ev)
    if sub is None:
        raise Retryable("subscription not found")
    sub.last_event_at = now
    from saas_core.billing import dunning
    if dunning.on_payment_failed(sub, now):  # False for canceled/expired subscriptions: no mail after a cancel
        frappe.enqueue("saas_core.notifications.billing_mail.send_payment_failed", invoice=None, subscription=sub.get("name"), reason=ev.get("reason"), enqueue_after_commit=True)


def _canceled(ev, p, now):
    sub = _find_sub(ev)
    if sub is None:
        raise Retryable("subscription not found")
    eff = sub.get("cancel_effective_at")
    if eff:
        from frappe.utils import get_datetime
        if get_datetime(eff) > _utc_now():  # day-based cancel: the day-end sweep marks it canceled
            sub.last_event_at = now
            sub.save(ignore_permissions=True)
            return
    sub.status = "canceled"
    sub.last_event_at = now
    sub.save(ignore_permissions=True)
    if sub.get("tenant"):
        from saas_core import jobs
        jobs.enqueue_job(sub.get("tenant"), "suspend", args={"reason": "canceled"},
                         dedupe_key=f"suspend:canceled:{sub.get('tenant')}")
        t = frappe.get_doc("Tenant", sub.get("tenant"))
        t.suspend_reason = "canceled"
        t.save(ignore_permissions=True)


def _updated(ev, p, now):
    sub = _find_sub(ev)
    if sub is None:
        raise Retryable("subscription not found")
    if ev.get("period_end"):
        sub.period_end = ev["period_end"]
    new_plan = p.get("plan")
    if new_plan and new_plan != sub.get("plan"):
        sub.plan = new_plan
        if sub.get("tenant"):
            t = frappe.get_doc("Tenant", sub.get("tenant"))
            t.plan = new_plan
            t.save(ignore_permissions=True)
    sub.last_event_at = now
    sub.save(ignore_permissions=True)


def _refund(ev, p, now):
    inner = p.get("payload") if isinstance(p.get("payload"), dict) else {}
    rent = (inner.get("refund") or {}).get("entity") if isinstance(inner.get("refund"), dict) else None
    rent = rent if isinstance(rent, dict) else {}
    gw = ev.get("gateway")
    rid, payid = rent.get("id"), rent.get("payment_id")
    amount = int(rent.get("amount") or 0)
    rdoc = None
    if rid:
        rows = frappe.get_all("Refund", filters={"gateway_refund_id": rid}, pluck="name")
        rdoc = frappe.get_doc("Refund", rows[0]) if rows else None
    if rdoc is None:  # webhook beat the commit of gateway_refund_id: match on what we sent (receipt/notes)
        rnotes = rent.get("notes") if isinstance(rent.get("notes"), dict) else {}
        for key in (rnotes.get("refund_doc"), rent.get("receipt")):
            if key and frappe.db.exists("Refund", key):
                c = frappe.get_doc("Refund", key)
                if not payid or not c.get("gateway_payment_id") or c.get("gateway_payment_id") == payid:
                    rdoc = c
                    break
    if rdoc is None and payid:
        for n in frappe.get_all("Refund", filters={"gateway_payment_id": payid, "status": "pending"}, pluck="name"):
            c = frappe.get_doc("Refund", n)
            if not c.get("gateway_refund_id") and int(c.get("amount") or 0) == amount:
                rdoc = c
                break
    if rdoc is not None:
        _apply_refund(rdoc, rent, p.get("event"), rid, now)
        return
    iid = ev.get("invoice_ref")
    rows = frappe.get_all("Invoice", filters={"gateway": gw, "gateway_invoice_id": iid}, pluck="name") if iid else []
    if rows:  # legacy: refund referencing an invoice directly
        inv = frappe.get_doc("Invoice", rows[0])
        inv.status = "refunded"
        inv.save(ignore_permissions=True)
    # else: e.g. a refund made from the gateway dashboard; nothing to reconcile, never fail the event


def _apply_refund(rdoc, rent, event_name, rid, now):
    if rdoc.get("status") == "processed":
        return  # idempotent
    status = rent.get("status")
    if event_name == "refund.failed" or status == "failed":
        new = "failed"
    elif event_name == "refund.processed" or status == "processed":
        new = "processed"
    else:
        return  # refund.created / pending: nothing to record yet
    if rid and not rdoc.get("gateway_refund_id"):
        rdoc.gateway_refund_id = rid
    rdoc.status = new
    rdoc.processed_at = now
    if new == "failed":
        err = rent.get("error_description") or rent.get("error_reason") or "refund failed"
        rdoc.error = str(err)[:140]
    rdoc.save(ignore_permissions=True)
    if new != "processed":
        return
    sid = rdoc.get("subscription")
    if sid and frappe.db.exists("Subscription", sid):
        sub = frappe.get_doc("Subscription", sid)
        from saas_core.billing import refunds
        refunds.book(sub, rdoc)
        sub.save(ignore_permissions=True)
    frappe.enqueue("saas_core.notifications.billing_mail.send_refund_processed", refund=rdoc.name,
                   enqueue_after_commit=True)


_HANDLERS = {
    "checkout.completed": _checkout_completed,
    "payment.succeeded": _payment_succeeded,
    "payment.failed": _payment_failed,
    "subscription.canceled": _canceled,
    "subscription.updated": _updated,
    "refund": _refund,
}


def _schedule_retry(ev, now, err):
    """Transient failure: stay `received`, count the attempt, record a short error and set the backoff deadline
    (honoured by `sweep_received_events`). After MAX_ATTEMPTS the event needs a human."""
    from datetime import timedelta
    n = int(ev.get("attempts") or 0) + 1
    ev.attempts = n
    ev.error = f"{type(err).__name__}: {err}"[:200]
    if n >= MAX_ATTEMPTS:
        ev.status = "needs_attention"
        ev.next_retry_at = None
    else:
        ev.status = "received"
        ev.next_retry_at = now + timedelta(seconds=RETRY_BACKOFF[min(n, len(RETRY_BACKOFF)) - 1])
    ev.save(ignore_permissions=True)


def process_payment_event(event: str | None = None, now=None, payment_event: str | None = None,
                          subscription_ref_hint: str | None = None) -> None:
    # `event` is a reserved frappe.enqueue kwarg (it is swallowed), so the queue passes payment_event.
    event = event or payment_event
    ev = frappe.get_doc("Payment Event", event)
    if ev.get("status") in ("processed", "ignored", "needs_attention"):
        return
    now = _now(now)
    from frappe.utils import get_datetime
    nra = ev.get("next_retry_at")
    if ev.get("status") == "received" and nra and get_datetime(nra) > now:
        return  # a stale queue job fired before its backoff elapsed; the sweep re-enqueues when due
    ev.status = "processing"
    ev.save(ignore_permissions=True)
    try:
        handler = _HANDLERS.get(ev.get("kind"))
        if handler is None:
            ev.status = "ignored"
        else:
            p = _payload(ev)
            view = _view(ev)
            if subscription_ref_hint and not view.get("subscription_ref"):
                view["subscription_ref"] = subscription_ref_hint  # HMAC-verified callback binds payment -> sub
            if ev.get("kind") in ("checkout.completed", "payment.succeeded"):
                from saas_core.audit import cache_lock
                lock_tenant = view.get("tenant_ref")
                if ev.get("kind") == "payment.succeeded":
                    sess = _resolve_payment_refs(view, p)  # before the lock: may call the gateway
                    lock_tenant = lock_tenant or (sess.get("tenant") if sess is not None else None)
                # same key as process_verified_callback (tenant), so webhook and callback serialize
                with cache_lock(f"saas_core:billing:{lock_tenant or view.get('subscription_ref')}"):
                    handler(view, p, now)
            else:
                handler(view, p, now)
            ev.status = "processed"
        ev.error = None
        ev.next_retry_at = None
        ev.processed_at = now
        ev.attempts = int(ev.get("attempts") or 0) + 1
        ev.save(ignore_permissions=True)
    except Retryable as e:
        _schedule_retry(ev, now, e)
        return
    except PaymentMismatch as e:
        ev.status = "needs_attention"
        ev.error = f"PaymentMismatch: {e.details}"[:200]
        ev.attempts = int(ev.get("attempts") or 0) + 1
        ev.save(ignore_permissions=True)
        _flag_mismatch(e, ev_name=ev.get("name"), tenant=ev.get("tenant_ref"))
        return
    except Exception as e:
        ev.status = "failed"
        ev.error = f"{type(e).__name__}: {e}"
        ev.attempts = int(ev.get("attempts") or 0) + 1
        ev.save(ignore_permissions=True)
        raise
    from saas_core import events
    events.publish(f"billing.{ev.get('kind')}", tenant=ev.get("tenant_ref"), data={"event": event})


def sweep_received_events(now=None) -> int:
    """Scheduler (every minute): re-enqueue `received` events that were never picked up (lost job, worker down) or
    whose retry backoff has elapsed. Events `received` for less than 2 minutes are left to the normal queue.
    Returns the number re-enqueued."""
    from datetime import timedelta
    from frappe.utils import get_datetime
    from frappe.utils import now_datetime
    # modified/creation are Frappe-managed (site-local); next_retry_at is ours (UTC)
    if now is None:
        local_now = now_datetime()
    else:  # a given `now` (naive UTC or aware) -> naive site-local
        from frappe.utils import convert_utc_to_system_timezone
        local_now = convert_utc_to_system_timezone(_now(now)).replace(tzinfo=None)
    cutoff = local_now - timedelta(seconds=SWEEP_AFTER_SECONDS)
    now = _now(now)
    n = 0
    for name in frappe.get_all("Payment Event", filters={"status": "received"}, pluck="name"):
        row = frappe.get_doc("Payment Event", name)
        nra = row.get("next_retry_at")
        if nra:
            due = get_datetime(nra) <= now
        else:
            last = row.get("modified") or row.get("creation")
            due = last is None or get_datetime(last) <= cutoff
        if due:
            frappe.enqueue("saas_core.billing.processor.process_payment_event", payment_event=name,
                           enqueue_after_commit=True)
            n += 1
    return n


class PriceNotFound(ValueError):
    """No Plan Price row matches the subscription's interval/currency. Never falls back to another currency."""


def _price_amount(sub):
    """Plan price (minor units) for the subscription's interval/currency; server-side, never client input.
    Raises PriceNotFound when no row matches both interval and currency (no cross-currency fallback)."""
    plan = frappe.get_doc("Plan", sub.get("plan"))
    currency = sub.get("currency")
    if not currency:
        raise PriceNotFound(f"subscription {sub.get('name')!r} has no currency")
    rows = [r for r in (plan.get("prices") or []) if r.get("interval") == sub.get("interval")
            and r.get("currency") == currency]
    rows = [r for r in rows if not r.get("gateway") or r.get("gateway") == sub.get("gateway")] or rows
    if not rows:
        raise PriceNotFound(f"no {currency} {sub.get('interval')} price for plan {sub.get('plan')!r}")
    return int(round(float(rows[0].get("amount") or 0))), rows[0].get("currency")


def _return_url():
    from saas_core.notifications.billing_mail import billing_url
    url = billing_url()
    if url.startswith("/"):
        from frappe.utils import get_url
        url = get_url().rstrip("/") + url
    return url


def _issue_invoice(sub, now):
    from saas_core.gateways import registry
    from frappe.utils import get_datetime
    amount, currency = _price_amount(sub)
    start = get_datetime(sub.get("period_end"))
    end = _period_after(start, sub.get("interval"))
    email = frappe.db.get_value("User", sub.get("owner_user"), "email") or sub.get("owner_user")
    gw = registry.get_gateway(sub.get("gateway") or "razorpay")
    from saas_core.gateways.base import require_capability
    require_capability(gw, "payment_link", "invoice-mode renewals need hosted payment links")
    link = gw.create_payment_link(
        amount=amount, currency=currency, description=f"{sub.get('plan')} plan renewal",
        email=email, notes={"tenant": sub.get("tenant"), "subscription": sub.get("name")},
        callback_url=_return_url(), reference_id=f"{sub.get('name')}-{start:%Y%m%d}")
    inv = frappe.get_doc({"doctype": "Invoice", "tenant": sub.get("tenant"), "owner_user": sub.get("owner_user"),
                          "subscription": sub.get("name"), "gateway": sub.get("gateway"),
                          "gateway_invoice_id": link.get("id"), "status": "open", "total_amount": amount,
                          "currency": currency, "issued_at": now, "period_start": start, "period_end": end,
                          "pdf_url": link.get("short_url") or ""})  # pdf_url doubles as the hosted payment URL
    inv.insert(ignore_permissions=True)
    return inv


def _record_issue_failure(sub_name, err):
    """Visible record of a renewal that could not be priced: audit entry (never raises into the sweep)."""
    try:
        from saas_core import audit
        audit.log("invoice.issue_failed", actor="system", target_doctype="Subscription", target=sub_name,
                  details={"reason": "price_not_found", "error": str(err)})
    except Exception:
        frappe.log_error(f"saas_core issue_due_invoices: audit failed for {sub_name}")


def issue_due_invoices(now=None) -> int:
    """Daily: for invoice-mode subscriptions whose period has ended, issue a Razorpay Payment Link + open Invoice.
    An invoice still unpaid a day after issue pushes the subscription into the dunning path. Returns invoices issued."""
    from frappe.utils import get_datetime
    from saas_core.billing import dunning
    from saas_core.experimental import enabled
    if not enabled("invoice_renewals"):  # EXPERIMENTAL: renewal payment links are not live-verified
        return 0
    now = _now(now)
    issued = 0
    names = frappe.get_all("Subscription", filters={"billing_mode": "invoice", "status": ["in", ["active", "past_due"]]},
                           pluck="name")
    for n in names:
        try:
            sub = frappe.get_doc("Subscription", n)
            if not sub.get("period_end") or get_datetime(sub.get("period_end")) > now or sub.get("cancel_at_period_end"):
                continue
            open_inv = frappe.get_all("Invoice", filters={"subscription": n, "status": "open"}, pluck="name")
            if open_inv:
                inv = frappe.get_doc("Invoice", open_inv[0])
                overdue = (now - get_datetime(inv.get("issued_at") or now)).total_seconds() >= 86400
                if overdue and sub.get("status") != "past_due":
                    dunning.on_payment_failed(sub, now)
                continue
            _issue_invoice(sub, now)
            issued += 1
        except PriceNotFound as e:
            frappe.log_error(f"saas_core issue_due_invoices: {e}")
            _record_issue_failure(n, e)
        except Exception:
            frappe.log_error(f"saas_core issue_due_invoices failed for {n}")
    return issued
