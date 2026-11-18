"""Subscription controller."""
import frappe
from frappe.model.document import Document

STATUSES = ("trialing", "active", "past_due", "canceled")


class Subscription(Document):
    def validate(self):
        if self.get("status") not in STATUSES:
            frappe.throw(f"Invalid status {self.get('status')}")
        getter = getattr(self, "get_doc_before_save", None)
        before = getter() if callable(getter) else None
        if before and before.get("status") == "canceled" and self.get("status") != "canceled":
            frappe.throw("A canceled subscription is terminal and cannot change status")
