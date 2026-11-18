"""Gateway Account controller."""
import frappe
from frappe.model.document import Document


class GatewayAccount(Document):
    def validate(self):
        if self.get("mode") not in ("test", "live"):
            frappe.throw("Mode must be test or live")
        if self.get("gateway") == "manual" and self.get("mode") == "live":
            frappe.throw("The manual (mock payment) gateway cannot be used in live mode")
        if self.get("gateway") == "fake" and self.get("mode") == "live":
            frappe.throw("The fake gateway cannot be used in live mode")
        key_id = (self.get("key_id") or "").strip()
        if key_id:
            if self.get("mode") == "live" and key_id.startswith("rzp_test_"):
                frappe.throw("Live mode cannot use a test key id (rzp_test_...)")
            if self.get("mode") == "test" and key_id.startswith("rzp_live_"):
                frappe.throw("Test mode cannot use a live key id (rzp_live_...)")
