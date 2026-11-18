"""Tenant Member controller: one row per invited/active team member of a tenant."""
import frappe
from frappe.model.document import Document

ROLES = ("owner", "admin", "member", "viewer")


class TenantMember(Document):
    def validate(self):
        if self.get("role") not in ROLES:
            frappe.throw("Invalid role.")
