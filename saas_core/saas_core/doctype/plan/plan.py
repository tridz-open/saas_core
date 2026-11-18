"""Plan controller."""
import frappe
from frappe.model.document import Document


class Plan(Document):
    def validate(self):
        seen = set()
        for row in self.get("prices") or []:
            if (row.get("amount") or 0) < 0:
                frappe.throw("Price amount must be >= 0")
            key = (row.get("interval"), row.get("currency"), row.get("gateway") or "")
            if key in seen:
                frappe.throw(f"Duplicate price for {key[0]}/{key[1]}/{key[2] or 'any gateway'}")
            seen.add(key)
