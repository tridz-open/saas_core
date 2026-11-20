"""Billing emails: payment failed, dunning, invoice paid, cancellation, refund processed, renewal receipt. Idempotent per (invoice|subscription)+kind via cache markers.
Templates come from get_setting("<key>_email_template"); a plain-text fallback is used when none is configured."""
import frappe

from saas_core import audit
from saas_core.billing import proration
from saas_core.config import get_setting
from saas_core.notifications import templating

MARKER_TTL = 90 * 24 * 3600
BILLING_POLICY_LINE = "Billed monthly in advance. If you cancel, we charge up to the end of that day and refund the unused days."


def format_amount(minor, currency) -> str:
    """Minor units (cents) -> '12.50 USD'. Zero-decimal currencies are not divided."""
    zero = {"JPY", "KRW", "VND", "CLP", "UGX", "XOF", "XAF"}
    symbols = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "SGD": "S$", "AUD": "A$", "CAD": "C$"}
    cur = (currency or "").upper()
    n = int(minor or 0)
    val = n if cur in zero else n / 100
    whole = float(val).is_integer()
    if cur == "INR":  # Indian grouping: 1,04,000
        rupees, paise = int(abs(val)), round((abs(val) % 1) * 100)
        head, tail = str(rupees)[:-3], str(rupees)[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:]); head = head[:-2]
        txt = ",".join(([head] if head else []) + groups + [tail]) if head or groups else tail
        if not whole:
            txt += f".{paise:02d}"
        txt = ("-" if val < 0 else "") + txt
    else:
        txt = f"{val:,.0f}" if whole else f"{val:,.2f}"
    sym = symbols.get(cur)
    return f"{sym}{txt}" if sym else f"{txt} {cur}".strip()


def billing_url() -> str:
    return templating.absolute_url((get_setting("console_url", "/console") or "/console").rstrip("/") + "/billing")


def _doc(dt, v):
    return frappe.get_doc(dt, v) if isinstance(v, str) else v


def _recipient(owner_user):
    if not owner_user:
        return None
    email = frappe.db.get_value("User", owner_user, "email")
    return email or (owner_user if "@" in str(owner_user) else None)


def _tenant_title(tenant):
    if not tenant or not frappe.db.exists("Tenant", tenant):
        return "your restaurant"
    return templating.display_title(frappe.get_doc("Tenant", tenant))


def _unpaid_amount(sub) -> str:
    """Amount of the latest unpaid invoice of a subscription ('' when none), for dunning / failed-payment mails."""
    if not sub:
        return ""
    try:
        rows = frappe.get_all("Invoice", filters={"subscription": sub.get("name"), "status": ["!=", "paid"]},
                              fields=["total_amount", "currency", "number", "name"], order_by="creation desc", limit=1)
    except Exception:  # noqa: BLE001
        return ""
    r = rows[0] if rows else None
    return format_amount(r.get("total_amount"), r.get("currency")) if r and r.get("total_amount") else ""


def _deliver(key, kind, marker, to, ctx, subject, text, ref_dt, ref_name):
    if not to:
        frappe.log_error(f"{kind} email skipped: no recipient for {marker}", "saas_core billing_mail")
        return False
    mkey = f"saas_core:billing_mail:{kind}:{marker}"
    # Atomic: marker check, send and marker set all happen under one lock, so concurrent retries cannot double-send.
    with audit.cache_lock(f"{mkey}:lock", ttl=60, wait=10):
        cache = frappe.cache()
        if cache.get_value(mkey):
            return False
        ctx = {"policy_line": BILLING_POLICY_LINE, "billing_url": billing_url(), "cta_url": ctx.get("invoice_url") or billing_url(), **ctx}
        rendered = templating.render(key, templating.brand_context(**ctx))
        subject, message = rendered if rendered else (subject, f"<p>{text}</p>")
        frappe.sendmail(recipients=[to], subject=subject, message=message,
                        reference_doctype=ref_dt, reference_name=ref_name, now=False)
        cache.set_value(mkey, 1, expires_in_sec=MARKER_TTL)
    return True


def send_payment_failed(invoice, reason=None, subscription=None):
    inv = _doc("Invoice", invoice) if invoice else None
    sub = _doc("Subscription", subscription or (inv or {}).get("subscription")) if (subscription or (inv or {}).get("subscription")) else None
    owner = (inv or sub or {}).get("owner_user")
    tenant = (inv or sub or {}).get("tenant")
    marker = (inv or {}).get("name") or (sub or {}).get("name")
    amount = format_amount((inv or {}).get("total_amount"), (inv or {}).get("currency")) if inv else _unpaid_amount(sub)
    ctx = {"amount": amount, "reason": reason or "", "tenant_title": _tenant_title(tenant),
           "invoice_number": (inv or {}).get("number") or (inv or {}).get("name") or "",
           "next_retry": templating.fmt_date((sub or {}).get("next_retry_at"), utc=True),
           "suspend_date": templating.fmt_date((sub or {}).get("suspend_date"), utc=True)}
    text = (f"Your payment of {amount} failed" if amount else "Your payment failed") + (f": {reason}" if reason else "") + f". Update your payment method: {billing_url()}. {BILLING_POLICY_LINE}"
    return _deliver("payment_failed", "payment_failed", marker, _recipient(owner), ctx,
                    "Payment failed", text, "Invoice" if inv else "Subscription", marker)


def send_dunning(subscription, attempt=1):
    sub = _doc("Subscription", subscription)
    to = _recipient(sub.get("owner_user"))
    ctx = {"attempt": attempt, "is_final": int(attempt or 0) >= 3, "tenant_title": _tenant_title(sub.get("tenant")),
           "next_retry": templating.fmt_date(sub.get("next_retry_at"), utc=True),
           "suspend_date": templating.fmt_date(sub.get("suspend_date"), utc=True),
           "amount": _unpaid_amount(sub)}
    text = f"Your payment is overdue (notice {attempt}). Please update billing: {billing_url()}. {BILLING_POLICY_LINE}"
    return _deliver("dunning", "dunning", f"{sub.get('name')}:{attempt}", to, ctx,
                    "Payment overdue", text, "Subscription", sub.get("name"))


def send_invoice_paid(invoice):
    inv = _doc("Invoice", invoice)
    amount = format_amount(inv.get("total_amount"), inv.get("currency"))
    url = billing_url()
    start, end = templating.fmt_date(inv.get("period_start"), utc=True), templating.fmt_date(inv.get("period_end"), utc=True)
    sub_plan = ""
    if inv.get("subscription"):
        try:
            sub_plan = frappe.db.get_value("Subscription", inv.get("subscription"), "plan") or ""
        except Exception:  # noqa: BLE001
            sub_plan = ""
    ctx = {"amount": amount, "invoice_number": inv.get("number") or inv.get("name"),
           "paid_date": templating.fmt_date(inv.get("paid_at") or inv.get("issued_at"), utc=True) or templating.fmt_date(proration.naive_utc_now(), utc=True),
           "plan_label": templating.plan_label(sub_plan), "period_start": start, "period_end": end,
           "period": f"{start} to {end}" if start and end else end,
           "invoice_url": f"{url}/invoices/{inv.get('name')}", "tenant_title": _tenant_title(inv.get("tenant"))}
    text = f"Payment received: invoice {inv.get('name')} for {amount} is paid. View your receipt: {ctx['invoice_url']}. {BILLING_POLICY_LINE}"
    return _deliver("invoice", "invoice_paid", inv.get("name"), _recipient(inv.get("owner_user")), ctx,
                    f"Receipt: {amount} paid", text, "Invoice", inv.get("name"))


def _payment_method(refund) -> str:
    gw = str(refund.get("gateway") or "").strip()
    return "Original payment method" + (f" ({gw.title()})" if gw else "")


def _stops_at_text(effective_at) -> str:
    """'11:59 pm IST on 18 Nov 2026' from the exclusive end instant (UTC-naive) minus one second."""
    import datetime as _dt
    from saas_core.billing import proration
    if not effective_at:
        return ""
    if isinstance(effective_at, str):
        try:
            effective_at = _dt.datetime.fromisoformat(effective_at.strip().replace("Z", "")[:19])
        except ValueError:
            return ""
    last = proration.to_aware(effective_at).astimezone(proration.IST) - _dt.timedelta(seconds=1)
    return f"{last.strftime('%I:%M %p').lstrip('0').lower()} IST on {last.day} {last.strftime('%b %Y')}"


def send_cancellation(subscription, refund=None):
    sub = _doc("Subscription", subscription)
    ref = _doc("Refund", refund) if refund else None
    cur = (ref or {}).get("currency") or sub.get("currency")
    has_refund = bool(ref and int(ref.get("amount") or 0) > 0)
    refund_amount = format_amount(ref.get("amount"), cur) if has_refund else ""
    paid = sub.get("paid_amount")
    charge = ""
    if has_refund and paid not in (None, ""):
        charge = format_amount(max(int(paid) - int(ref.get("amount") or 0), 0), cur)
    days = ref.get("days_used") if ref else None
    ctx = {"tenant_title": _tenant_title(sub.get("tenant")), "plan_label": templating.plan_label(sub.get("plan")),
           "stops_at_text": _stops_at_text(sub.get("cancel_effective_at")),
           "days_used": "" if days is None else days, "charge_amount": charge,
           "refund_amount": refund_amount, "has_refund": has_refund}
    text = (f"Your subscription is canceled. Your site stays active until {ctx['stops_at_text']}."
            + (f" We are refunding {refund_amount} to your original payment method; refunds usually take 5 to 7 business days." if has_refund else "")
            + f" {BILLING_POLICY_LINE}")
    return _deliver("cancellation", "canceled", sub.get("name"), _recipient(sub.get("owner_user")), ctx,
                    "Your subscription is canceled" + (f": {refund_amount} refund on the way" if has_refund else ""),
                    text, "Subscription", sub.get("name"))


def send_refund_processed(refund):
    ref = _doc("Refund", refund)
    amount = format_amount(ref.get("amount"), ref.get("currency"))
    ctx = {"refund_amount": amount, "payment_method": _payment_method(ref),
           "refund_date": templating.fmt_date(ref.get("processed_at"), utc=True) or templating.fmt_date(proration.naive_utc_now(), utc=True),
           "reference": ref.get("gateway_refund_id") or ref.get("name"),
           "tenant_title": _tenant_title(ref.get("tenant"))}
    text = (f"Your refund of {amount} has been processed to your original payment method. Refunds usually take 5 to 7 "
            f"business days to show up. {BILLING_POLICY_LINE}")
    return _deliver("refund_processed", "refund_processed", ref.get("name"), _recipient(ref.get("owner_user")), ctx,
                    f"Your refund of {amount} has been processed", text, "Refund", ref.get("name"))


def send_renewal_receipt(invoice):
    inv = _doc("Invoice", invoice)
    amount = format_amount(inv.get("total_amount"), inv.get("currency"))
    url = billing_url()
    start, end = templating.fmt_date(inv.get("period_start"), utc=True), templating.fmt_date(inv.get("period_end"), utc=True)
    sub_plan, next_renewal = "", ""
    if inv.get("subscription"):
        try:
            sub_plan = frappe.db.get_value("Subscription", inv.get("subscription"), "plan") or ""
            next_renewal = templating.fmt_date(frappe.db.get_value("Subscription", inv.get("subscription"), "period_end"), utc=True)
        except Exception:  # noqa: BLE001
            pass
    ctx = {"amount": amount, "invoice_number": inv.get("number") or inv.get("name"),
           "plan_label": templating.plan_label(sub_plan), "period_start": start, "period_end": end,
           "period": f"{start} to {end}" if start and end else end, "next_renewal_date": next_renewal,
           "invoice_url": f"{url}/invoices/{inv.get('name')}", "tenant_title": _tenant_title(inv.get("tenant"))}
    text = (f"Renewal receipt: invoice {inv.get('name')} for {amount} is paid."
            + (f" Next renewal on {next_renewal}." if next_renewal else "")
            + f" View your receipt: {ctx['invoice_url']}. If a renewal payment fails we retry and send reminders before the site is paused. {BILLING_POLICY_LINE}")
    return _deliver("renewal_receipt", "renewal_receipt", inv.get("name"), _recipient(inv.get("owner_user")), ctx,
                    f"Renewal receipt: {amount} paid", text, "Invoice", inv.get("name"))
