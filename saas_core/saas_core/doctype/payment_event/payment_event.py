"""Payment Event controller: immutable except status/error/processed_at/attempts."""
import frappe
from frappe.model.document import Document

MUTABLE = {"status", "error", "processed_at", "attempts", "next_retry_at"}
IGNORED = {"modified", "modified_by", "creation", "owner", "idx", "docstatus"}


class PaymentEvent(Document):
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
                frappe.throw(f"Payment Event is immutable: cannot change {key}")
