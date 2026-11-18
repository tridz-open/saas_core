"""Backfill legacy subscriptions (paid before gateway_payment_id / paid_amount / period_start were recorded).

    bench --site <site> execute saas_core.billing.backfill.legacy_subscriptions                       # dry run
    bench --site <site> execute saas_core.billing.backfill.legacy_subscriptions --kwargs "{'dry_run': False}"

Recovery order: paid Invoice -> Payment Event payloads / Checkout Session (local, no network) -> Razorpay
(`order_payments` for order ids, `subscription_invoices` for subscription ids; only when use_gateway=True, which is the
default for the manual command and False for the migrate patch). Only empty fields are filled, so re-runs are no-ops.
Relies on Razorpay: orders/{id}/payments[].id|status|amount, invoices?subscription_id=[].payment_id|amount_paid|status,
payments/{id}.amount, and locally Invoice.gateway_invoice_id/gateway_payment_id, Subscription.gateway_sub_id,
Checkout Session.session_ref/amount_minor, Payment Event.raw_payload (razorpay_payment_id / payload.payment.entity).
"""
from __future__ import annotations

import frappe

from saas_core.billing import cancellation


def _paid_invoice(sub):
    rows = frappe.get_all("Invoice", filters={"subscription": sub.get("name"), "status": "paid"},
                          fields=["name", "gateway_invoice_id", "gateway_payment_id", "total_amount", "currency"],
                          order_by="creation desc", limit=1)
    return rows[0] if rows else None


def _payment_ids_in(payload) -> list[str]:
    try:
        p = frappe.parse_json(payload) if isinstance(payload, str) and payload else (payload or {})
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(p, dict):
        return []
    ids = [p.get("razorpay_payment_id")]
    ent = ((p.get("payload") or {}).get("payment") or {}).get("entity") or {}
    ids.append(ent.get("id"))
    return [i for i in ids if i and str(i).startswith("pay_")]


def _event_payment(sub, order_id):
    """(payment_id, amount|None) from a stored Payment Event for this subscription / order, else (None, None)."""
    rows = frappe.get_all("Payment Event", filters={"gateway": sub.get("gateway")},
                          fields=["subscription_ref", "raw_payload"], order_by="creation desc", limit=500)
    for r in rows:
        raw = r.get("raw_payload") or ""
        if (sub.get("gateway_sub_id") and r.get("subscription_ref") == sub.get("gateway_sub_id")) or \
                (order_id and order_id in str(raw)):
            ids = _payment_ids_in(raw)
            if ids:
                return ids[0], None
    return None, None


def _order_id(sub, inv):
    for ref in ((inv or {}).get("gateway_invoice_id"),):
        if ref and str(ref).startswith("order_"):
            return ref
    sess = frappe.get_all("Checkout Session", filters={"subscription": sub.get("name")},
                          fields=["session_ref", "amount_minor"], limit=1)
    if sess and str(sess[0].get("session_ref") or "").startswith("order_"):
        return sess[0]["session_ref"]
    return None


def _from_gateway(sub, inv, order_id, gw):
    """(payment_id, amount) from Razorpay, or (None, None)."""
    if order_id:
        paid = [p for p in gw.order_payments(order_id) if p.get("status") == "captured"]
        if paid:
            return paid[-1]["id"], int(paid[-1].get("amount") or 0)
    if sub.get("gateway_sub_id"):
        paid = [i for i in gw.subscription_invoices(sub.get("gateway_sub_id"))
                if i.get("status") == "paid" and i.get("payment_id")]
        match = [i for i in paid if inv and i.get("id") == inv.get("gateway_invoice_id")]
        pick = (match or paid[:1])
        if pick:
            return pick[0]["payment_id"], int(pick[0].get("amount_paid") or 0) or None
    return None, None


def _plan(sub, use_gateway, gw_factory) -> dict | None:
    need_pid, need_amt = not sub.get("gateway_payment_id"), not sub.get("paid_amount")
    need_start = not sub.get("period_start") and bool(sub.get("period_end"))
    if not (need_pid or need_amt or need_start):
        return None
    inv = _paid_invoice(sub)
    changes, sources, error = {}, [], None
    if need_start:
        changes["period_start"] = cancellation._minus_cycle(cancellation._dt_of(sub.get("period_end")), sub.get("interval"))
    if need_pid or need_amt:
        pid, amt = sub.get("gateway_payment_id") or (inv or {}).get("gateway_payment_id"), None
        order_id = _order_id(sub, inv)
        if pid:
            sources.append("invoice")
        else:
            pid, amt = _event_payment(sub, order_id)
            if pid:
                sources.append("payment_event")
        if not pid and use_gateway:
            try:
                pid, amt = _from_gateway(sub, inv, order_id, gw_factory())
                if pid:
                    sources.append("gateway")
            except Exception as e:  # noqa: BLE001
                error = f"gateway lookup failed: {type(e).__name__}: {e}"[:140]
        if pid and need_pid:
            changes["gateway_payment_id"] = pid
        if pid and need_amt:
            if amt is None and use_gateway and "gateway" not in sources:
                try:
                    amt = int(gw_factory().get_payment(pid).get("amount") or 0) or None
                    sources.append("gateway")
                except Exception as e:  # noqa: BLE001
                    error = f"payment fetch failed: {type(e).__name__}: {e}"[:140]
            if amt is None and inv:
                amt = int(inv.get("total_amount") or 0) or None
                sources.append("invoice_total")
            if amt:
                changes["paid_amount"] = int(amt)
        if not pid and not error:
            error = "payment id unresolved" + ("" if use_gateway else " locally (needs manual gateway run)")
    if not changes and not error:
        return None
    return {"subscription": sub.get("name"), "tenant": sub.get("tenant"), "changes": changes,
            "sources": sources, "invoice": (inv or {}).get("name"), "error": error}


def legacy_subscriptions(dry_run=True, use_gateway=True, actor="Administrator") -> list[dict]:
    """Returns the plan/outcome list (one entry per subscription needing work); prints it on dry runs."""
    cache: dict = {}

    def gw_factory(sub_gateway="razorpay"):
        if "gw" not in cache:
            from saas_core.gateways.registry import get_gateway
            cache["gw"] = get_gateway("razorpay")
        return cache["gw"]

    out = []
    for name in frappe.get_all("Subscription", filters={"status": "active"}, pluck="name"):
        sub = frappe.get_doc("Subscription", name)
        gw_ok = use_gateway and sub.get("gateway") == "razorpay"
        item = _plan(sub, gw_ok, gw_factory)
        if not item:
            continue
        item["applied"] = False
        if not dry_run and item["changes"]:
            before = {k: (str(sub.get(k)) if sub.get(k) is not None else None) for k in item["changes"]}
            for k, v in item["changes"].items():
                setattr(sub, k, v)
            sub.save(ignore_permissions=True)
            pid = item["changes"].get("gateway_payment_id")
            if pid and item["invoice"] and not frappe.db.get_value("Invoice", item["invoice"], "gateway_payment_id"):
                frappe.db.set_value("Invoice", item["invoice"], "gateway_payment_id", pid)
            from saas_core import audit
            audit.log("subscription.backfill_legacy", actor=actor, target_doctype="Subscription", target=name,
                      details={"before": before, "after": {k: str(v) for k, v in item["changes"].items()},
                               "sources": item["sources"]})
            item["applied"] = True
        out.append(item)
    if dry_run:
        for it in out:
            print(f"[dry-run] {it['subscription']} ({it['tenant']}): "
                  f"{ {k: str(v) for k, v in it['changes'].items()} or 'no change' } via {it['sources']}"
                  + (f" ! {it['error']}" if it["error"] else ""))
        print(f"[dry-run] {len(out)} subscription(s) need backfill; re-run with --kwargs \"{{'dry_run': False}}\"")
    return out


def capture_authorized(dry_run=True, actor="Administrator") -> list[dict]:
    """Capture Razorpay payments of paid-marked subscriptions/invoices that are still `authorized`.

        bench --site <site> execute saas_core.billing.backfill.capture_authorized
        bench --site <site> execute saas_core.billing.backfill.capture_authorized --kwargs "{'dry_run': False}"

    Idempotent: captured payments are skipped, an "already captured" answer counts as success. Each capture is
    audited (`payment.capture_backfill`). Returns [{payment_id, subscription, invoice, status, action, error}].
    """
    from saas_core.gateways.registry import get_gateway
    gw = get_gateway("razorpay")
    refs: dict[str, dict] = {}
    for r in frappe.get_all("Subscription", filters={"gateway": "razorpay", "status": ["in", ["active", "trialing", "past_due"]]},
                            fields=["name", "gateway_payment_id"]):
        if r.get("gateway_payment_id"):
            refs.setdefault(r["gateway_payment_id"], {})["subscription"] = r["name"]
    for r in frappe.get_all("Invoice", filters={"gateway": "razorpay", "status": "paid"},
                            fields=["name", "gateway_payment_id"]):
        if r.get("gateway_payment_id"):
            refs.setdefault(r["gateway_payment_id"], {})["invoice"] = r["name"]
    out = []
    for pid, ref in refs.items():
        item = {"payment_id": pid, "subscription": ref.get("subscription"), "invoice": ref.get("invoice"),
                "status": None, "action": "none", "error": None}
        try:
            pay = gw.http.request("GET", f"https://api.razorpay.com/v1/payments/{pid}", basic_auth=gw._auth()) or {}
            item["status"] = pay.get("status")
            if pay.get("status") == "authorized":
                if dry_run:
                    item["action"] = "would_capture"
                else:
                    if not pay.get("currency"):
                        raise ValueError("payment has no currency")  # never default; recorded as an error item
                    gw.capture_payment(pid, int(pay.get("amount") or 0), pay.get("currency"))
                    item["action"] = "captured"
                    from saas_core import audit
                    audit.log("payment.capture_backfill", actor=actor, target_doctype="Subscription",
                              target=ref.get("subscription") or ref.get("invoice"),
                              details={"payment_id": pid, "amount": pay.get("amount"), "invoice": ref.get("invoice")})
        except Exception as e:  # noqa: BLE001
            item["action"], item["error"] = "error", f"{type(e).__name__}: {e}"[:300]
        if item["action"] != "none":
            out.append(item)
    if dry_run:
        for it in out:
            print(f"[dry-run] {it['payment_id']} sub={it['subscription']} inv={it['invoice']}: {it['action']} {it['error'] or ''}")
        print(f"[dry-run] {len(out)} payment(s) need capture; re-run with --kwargs \"{{'dry_run': False}}\"")
    return out
