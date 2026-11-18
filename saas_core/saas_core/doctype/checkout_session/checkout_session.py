"""Checkout Session: server-side record of what a gateway session_ref was created for (tenant, plan, price,
interval, billing mode, amount, user). The processor and verify_payment trust this, never client-sent notes.
Immutable except `subscription` and `consumed_at`."""
import frappe
from frappe.model.document import Document

MUTABLE = {"subscription", "consumed_at"}
IGNORED = {"modified", "modified_by", "creation", "owner", "idx", "docstatus"}


class CheckoutSession(Document):
    def validate(self):
        getter = getattr(self, "get_doc_before_save", None)
        before = getter() if callable(getter) else None
        if not before:
            return
        old_map = before.as_dict() if hasattr(before, "as_dict") else dict(before)
        for key, old in old_map.items():
            if key in MUTABLE or key in IGNORED or key.startswith("_"):
                continue
            if self.get(key) != old:
                frappe.throw(f"Checkout Session is immutable: cannot change {key}")
