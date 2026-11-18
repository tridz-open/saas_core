"""Provision Job controller: state enum and terminal-state immutability."""
import frappe
from frappe.model.document import Document

STATES = ("pending", "dispatching", "submitted", "running", "succeeded", "failed", "stuck", "outcome_unknown", "needs_attention")
TERMINAL_STATES = frozenset({"succeeded", "failed"})


class ProvisionJob(Document):
    def validate(self):
        state = self.get("state") or "pending"
        if state not in STATES:
            frappe.throw(f"Invalid job state '{state}'.")
        getter = getattr(self, "get_doc_before_save", None)
        old = getter() if callable(getter) else None
        if old and old.get("state") in TERMINAL_STATES:
            changed = [k for k in ("state", "kind", "tenant", "args", "error", "attempts", "workflow_id", "approval", "dedupe_key")
                       if old.get(k) != self.get(k)]
            if changed:
                frappe.throw(f"Job is {old.get('state')} and immutable (changed: {', '.join(changed)}).")
