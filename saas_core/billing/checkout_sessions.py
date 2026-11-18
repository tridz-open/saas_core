"""Server-side Checkout Session records: gateway session_ref -> {tenant, plan, price, interval, billing mode,
amount, user}. The only trusted source for those facts once a payment comes back (callback or webhook)."""
from __future__ import annotations

import frappe

DOCTYPE = "Checkout Session"
FIELDS = ("tenant", "owner_user", "plan", "interval", "currency", "billing_mode", "checkout_mode",
          "amount_minor", "gateway_price_id", "price_row")


def session_key(gateway: str, ref: str) -> str:
    return f"{gateway}:{ref}"


def store_session(gateway: str, ref: str, **data):
    """Create the record (first write wins; an existing one is returned untouched)."""
    if not gateway or not ref or not data.get("tenant"):
        return None
    key = session_key(gateway, ref)
    if frappe.db.exists(DOCTYPE, key):
        return frappe.get_doc(DOCTYPE, key)
    doc = frappe.get_doc({"doctype": DOCTYPE, "name": key, "session_key": key, "gateway": gateway,
                          "session_ref": str(ref), **{k: v for k, v in data.items() if k in FIELDS}})
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return frappe.get_doc(DOCTYPE, key)
    return doc


def find_session(gateway: str, *refs):
    for ref in refs:
        if ref and frappe.db.exists(DOCTYPE, session_key(gateway, str(ref))):
            return frappe.get_doc(DOCTYPE, session_key(gateway, str(ref)))
    return None
