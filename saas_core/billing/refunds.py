"""Shared refund helpers: split a refund across the payments that funded a cycle, create/issue Refund docs.

A cycle can be funded by several payments: the original (Subscription.gateway_payment_id) plus upgrade-difference
payment-link payments, kept in Subscription.extra_payments (JSON list of {"payment_id","amount"}, oldest first).
A refund never exceeds what a single payment can give back, so it is split newest payment first.
"""
from __future__ import annotations

import json

import frappe

from saas_core.billing import proration

MAX_REFUND_ATTEMPTS = 5


def load_extras(sub) -> list[dict]:
    raw = sub.get("extra_payments")
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        return []
    return [{"payment_id": str(e.get("payment_id")), "amount": int(e.get("amount") or 0)}
            for e in data if isinstance(e, dict) and e.get("payment_id")]


def save_extras(sub, extras: list[dict]) -> None:
    sub.extra_payments = json.dumps(extras) if extras else ""


def add_extra(sub, payment_id, amount) -> None:
    """Record an additional payment funding the held amount (idempotent per payment id)."""
    if not payment_id or int(amount or 0) <= 0:
        return
    extras = load_extras(sub)
    if any(e["payment_id"] == payment_id for e in extras):
        return
    extras.append({"payment_id": payment_id, "amount": int(amount)})
    save_extras(sub, extras)


def split(sub, amount: int, base_payment_id, held: int):
    """-> (parts [(payment_id|None, amount)], remaining_extras, unallocated). Extras newest first, then the original
    payment capped at held less the extras. Does not mutate `sub`."""
    extras = load_extras(sub)
    left = [dict(e) for e in extras]
    remaining, parts = int(amount), []
    for e in reversed(left):
        if remaining <= 0:
            break
        take = min(remaining, e["amount"])
        if take > 0:
            parts.append((e["payment_id"], take))
            e["amount"] -= take
            remaining -= take
    cap = max(0, int(held) - sum(e["amount"] for e in extras))
    take = min(remaining, cap)
    if take > 0:
        parts.append((base_payment_id or None, take))
        remaining -= take
    return parts, [e for e in left if e["amount"] > 0], remaining


def base_payment_id(sub):
    """Payment that funded the cycle: Subscription.gateway_payment_id, else (legacy rows) the latest paid Invoice's."""
    if sub.get("gateway_payment_id"):
        return sub.get("gateway_payment_id")
    for filters in ({"subscription": sub.get("name"), "status": "paid"}, {"tenant": sub.get("tenant"), "status": "paid"}):
        rows = frappe.get_all("Invoice", filters=filters, fields=["gateway_payment_id"], order_by="creation desc", limit=1)
        if rows:
            return rows[0].get("gateway_payment_id") or None
    return None


def attempts(doc) -> int:
    err = doc.get("error") or ""
    if err.startswith("attempt "):
        try:
            return int(err.split(":", 1)[0].split()[1])
        except (ValueError, IndexError):
            return 1
    return 1 if err else 0


def flag(sub, why) -> None:
    from saas_core import events
    frappe.log_error(f"subscription {sub.get('name')}: {why}", "saas_core needs_attention")
    events.publish("billing.needs_attention", tenant=sub.get("tenant"),
                   data={"subscription": sub.get("name"), "reason": why})


def book(sub, doc) -> None:
    """The one place a processed refund is added to Subscription.refunded_amount (never for pending/failed)."""
    sub.refunded_amount = int(sub.get("refunded_amount") or 0) + int(doc.get("amount") or 0)


def mark_processed(sub, doc, now_naive) -> None:
    doc.status = "processed"
    doc.processed_at = now_naive
    doc.error = None
    book(sub, doc)


def create_doc(sub, *, user, kind, amount, currency, payment_id, reason, invoice=None, days_used=None,
               days_unused=None, now=None, status="pending", error=None):
    fields = {"doctype": "Refund", "subscription": sub.get("name"), "tenant": sub.get("tenant"),
              "owner_user": user, "invoice": invoice, "gateway": sub.get("gateway"),
              "gateway_payment_id": payment_id, "amount": int(amount), "currency": currency, "kind": kind,
              "status": status, "reason": reason, "requested_at": now or proration.to_naive_utc(proration.utc_now())}
    if days_used is not None:
        fields.update(days_used=days_used, days_unused=days_unused)
    if error:
        fields["error"] = error
    doc = frappe.get_doc(fields)
    doc.insert(ignore_permissions=True)
    return doc


def _norm_amount(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _find_existing(gateway, doc):
    """A refund Razorpay already holds for this Refund doc (matched on receipt or notes.refund_doc), or None.
    Raises when the lookup itself fails: the caller must not create blindly then."""
    fn = getattr(gateway, "list_payment_refunds", None)
    if not callable(fn):
        return None
    for r in fn(doc.get("gateway_payment_id")) or []:
        notes = r.get("notes") if isinstance(r.get("notes"), dict) else {}
        if doc.get("name") and doc.get("name") in (r.get("receipt"), notes.get("refund_doc")):
            return r
    return None


def _cap_exceeded(gateway, doc) -> str | None:
    """Reason string when refunding doc.amount would exceed what the payment can still give back, else None.
    Uses the larger of local rows (pending/processed, excluding this doc) and Razorpay's amount_refunded."""
    fn = getattr(gateway, "get_payment", None)
    if not callable(fn):
        return None
    pay = fn(doc.get("gateway_payment_id")) or {}
    total = _norm_amount(pay.get("amount"))
    if total <= 0:
        return None
    cur = str(doc.get("currency") or "").upper()
    pay_cur = str(pay.get("currency") or "").upper()
    if not cur:
        return "refund has no currency"
    if pay_cur and pay_cur != cur:
        return f"refund currency {cur} differs from payment currency {pay_cur}"
    local = 0
    for n in frappe.get_all("Refund", filters={"gateway_payment_id": doc.get("gateway_payment_id"),
                                               "status": ["in", ["pending", "processed"]]}, pluck="name"):
        if n != doc.get("name"):
            other = frappe.get_doc("Refund", n)
            if str(other.get("currency") or "").upper() not in ("", cur):
                return f"refund currency {cur} differs from earlier refund {n} ({other.get('currency')})"
            local += _norm_amount(other.get("amount"))
    done = max(local, _norm_amount(pay.get("amount_refunded")))
    if done + int(doc.get("amount")) > total:
        return f"refund exceeds payment: {done} already refunded of {total}"
    return None


def issue(doc, sub, gateway, now=None) -> bool:
    """Call the gateway for a Refund doc; True unless it failed. Never raises. Books refunded_amount on `sub`
    (caller saves it) only when the gateway answers processed.

    Idempotency: every create carries receipt=<doc name> and notes.refund_doc. A doc that was tried before (or
    already holds a gateway id) first asks Razorpay for the payment's refunds and adopts a match rather than
    creating a second refund; if that lookup fails we do not create."""
    now_n = proration.to_naive_utc(now or proration.utc_now())
    try:
        res = None
        if doc.get("gateway_refund_id") or attempts(doc) > 0:
            res = _find_existing(gateway, doc)
        if res is None:
            cap = getattr(gateway, "ensure_captured", None)
            if callable(cap):  # an authorized (uncaptured) payment cannot be refunded: capture first
                cap(doc.get("gateway_payment_id"))
            reason = _cap_exceeded(gateway, doc)
            if reason:
                doc.status = "failed"
                doc.error = f"attempt {MAX_REFUND_ATTEMPTS}: {reason}"[:140]  # not retryable
                doc.save(ignore_permissions=True)
                flag(sub, "refund_failed")
                return False
            res = gateway.refund_payment(doc.get("gateway_payment_id"), int(doc.get("amount")),
                                         notes={"tenant": doc.get("tenant"), "subscription": doc.get("subscription"),
                                                "reason": doc.get("kind") or "cancel",
                                                "refund_doc": doc.get("name")}, receipt=doc.get("name")) or {}
    except Exception as e:  # noqa: BLE001
        n = attempts(doc) + 1
        doc.status = "failed"
        doc.error = f"attempt {n}: {type(e).__name__}: {e}"[:140]
        doc.save(ignore_permissions=True)
        frappe.log_error(f"saas_core refund failed {doc.get('name')} (attempt {n})", "saas_core refund")
        if n == 1 or n >= MAX_REFUND_ATTEMPTS:
            flag(sub, "refund_failed")
        return False
    doc.gateway_refund_id = res.get("id")
    if res.get("status") == "processed":
        mark_processed(sub, doc, now_n)
    elif res.get("status") == "failed":
        doc.status = "failed"
        doc.error = f"attempt {attempts(doc) + 1}: gateway rejected the refund"
    else:
        doc.status = "pending"
        doc.error = None
    doc.save(ignore_permissions=True)
    return doc.status != "failed"


def summarize(docs):
    """One response dict for one or more Refund docs (first id, summed amount, worst status)."""
    if not docs:
        return None
    sts = {d.get("status") for d in docs}
    status = "failed" if "failed" in sts else ("pending" if "pending" in sts else "processed")
    return {"id": docs[0].get("name"), "status": status,
            "amount_minor": sum(int(d.get("amount") or 0) for d in docs), "currency": docs[0].get("currency")}
