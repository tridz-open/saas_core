"""Customer-selectable plan/template rules shared by tenants.create/duplicate and checkout.create_session."""
from __future__ import annotations

import frappe

from ._common import ApiError


def is_operator(user: str | None = None) -> bool:
    """SaaS Admin / System Manager / Administrator may use hidden plans and templates."""
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool({"SaaS Admin", "System Manager"} & set(frappe.get_roles(user) or []))


def check_selectable(user, plan, template=None):
    """Raise 422 unless `plan` (and `template`) may be chosen by `user`. Operators bypass."""
    if is_operator(user):
        return
    if not frappe.db.get_value("Plan", plan, "is_public") or not frappe.db.get_value("Plan", plan, "available"):
        raise ApiError("validation_error", "That plan is not available", 422, {"plan": "plan not available"})
    if not template:
        return
    if not frappe.db.get_value("Template", template, "is_public"):
        raise ApiError("validation_error", "That template is not available", 422, {"template": "template not available"})
    min_plan = frappe.db.get_value("Template", template, "min_plan")
    if min_plan:
        want = frappe.db.get_value("Plan", min_plan, "sort_order") or 0
        have = frappe.db.get_value("Plan", plan, "sort_order") or 0
        if plan != min_plan and have < want:
            raise ApiError("validation_error", "That template requires a higher plan", 422,
                           {"template": "requires a higher plan"})
