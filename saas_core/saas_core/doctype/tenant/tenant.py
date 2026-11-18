"""Tenant controller: slug rules and status state machine."""
import re

import frappe
from frappe.model.document import Document

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$")
RESERVED_SLUGS = frozenset({
    "www", "app", "api", "admin", "root", "mail", "smtp", "ftp", "support", "help", "billing",
    "status", "docs", "blog", "cdn", "static", "assets", "control", "dashboard", "login",
    "signup", "staging", "test", "dev", "demo", "internal", "system", "saas", "platform",
})
TRANSITIONS = {
    "pending": {"provisioning", "failed", "terminating"},
    "provisioning": {"active", "failed", "terminating"},
    "active": {"suspended", "failed", "terminating"},
    "suspended": {"active", "failed", "terminating"},
    "failed": {"provisioning", "terminating"},
    "terminating": {"failed"},
}


def _old_value(doc, field):
    getter = getattr(doc, "get_doc_before_save", None)
    if callable(getter):
        old = getter()
        return old.get(field) if old else None
    if doc.get("name"):
        return frappe.db.get_value(doc.get("doctype"), doc.get("name"), field)
    return None


class Tenant(Document):
    def validate(self):
        self.validate_slug()
        self.validate_status_transition()

    def validate_slug(self):
        slug = self.get("slug") or ""
        if not SLUG_RE.match(slug):
            frappe.throw("Invalid slug: use 4-32 chars, lowercase letters, digits and hyphens, starting with a letter.")
        if slug in RESERVED_SLUGS:
            frappe.throw(f"Slug '{slug}' is reserved.")

    def validate_status_transition(self):
        new = self.get("status") or "pending"
        old = _old_value(self, "status")
        if old is None:
            if new != "pending":
                frappe.throw("New tenants must start in 'pending' status.")
            return
        if new == old:
            return
        if new not in TRANSITIONS.get(old, set()):
            frappe.throw(f"Invalid status transition {old} -> {new}.")
