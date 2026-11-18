"""Approval Request controller: two-person rule and status flow."""
import json

import frappe
from frappe.model.document import Document

FLOW = {
    "pending": {"approved", "expired"},
    "approved": {"executing", "expired"},
    "executing": {"executed", "failed", "outcome_unknown"},
    "outcome_unknown": {"executed", "failed"},
    "executed": set(),
    "failed": set(),
    "expired": set(),
}
DEFAULT_APPROVALS_REQUIRED = 2


def _required() -> int:
    try:
        from saas_core.provisioning.ports import APPROVALS_REQUIRED
        return int(APPROVALS_REQUIRED)
    except Exception:
        return DEFAULT_APPROVALS_REQUIRED


def _approvers(doc) -> list:
    v = doc.get("approved_by")
    if not v:
        return []
    return list(json.loads(v)) if isinstance(v, str) else list(v)


class ApprovalRequest(Document):
    def validate(self):
        approvers = _approvers(self)
        if self.get("requested_by") and self.get("requested_by") in approvers:
            frappe.throw("Approver cannot be the requester.")
        if len(set(approvers)) != len(approvers):
            frappe.throw("Each approver may approve only once.")
        status = self.get("status") or "pending"
        if status not in FLOW:
            frappe.throw(f"Invalid approval status '{status}'.")
        getter = getattr(self, "get_doc_before_save", None)
        old = getter() if callable(getter) else None
        old_status = old.get("status") if old else None
        if old_status and status != old_status and status not in FLOW[old_status]:
            frappe.throw(f"Invalid approval status transition {old_status} -> {status}.")
        if status in ("approved", "executing", "executed") and len(approvers) < _required():
            frappe.throw(f"At least {_required()} distinct approvers required.")
