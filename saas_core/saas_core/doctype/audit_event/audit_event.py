"""Audit Event controller: append-only."""
import frappe
from frappe.model.document import Document


class AuditEvent(Document):
    def on_update(self):
        if not (self.flags or {}).get("in_insert"):
            frappe.throw("Audit Events are immutable.")

    def on_trash(self):
        frappe.throw("Audit Events cannot be deleted.")
