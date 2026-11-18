"""Entitlement checks: may a user create a tenant on a plan right now."""
from __future__ import annotations

import frappe

from saas_core.config import get_setting

DEFAULT_MAX_TENANTS_PER_USER = 5


def capacity_open() -> bool:
    """Platform-wide switch (site_config `saas_core_capacity_open`, default True)."""
    v = get_setting("capacity_open")
    return True if v is None else bool(v)


def can_create_tenant(user: str, plan: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is '' when allowed."""
    if not plan or not frappe.db.exists("Plan", plan):
        return False, "plan_not_found"
    if not frappe.db.get_value("Plan", plan, "available"):
        return False, "plan_unavailable"
    if not capacity_open():
        return False, "capacity_closed"
    cap = int(get_setting("max_tenants_per_user", DEFAULT_MAX_TENANTS_PER_USER) or DEFAULT_MAX_TENANTS_PER_USER)
    n = len(frappe.get_all("Tenant", filters={"owner_user": user, "status": ("!=", "terminating")}, pluck="name"))
    if n >= cap:
        return False, "tenant_limit_reached"
    return True, ""


def _limits(plan: str) -> dict:
    import json
    raw = frappe.db.get_value("Plan", plan, "limits")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def plan_change_kind(old_plan: str, new_plan: str) -> str:
    """'upgrade' if new plan ranks higher by sort_order, else 'downgrade'."""
    o = frappe.db.get_value("Plan", old_plan, "sort_order") or 0
    n = frappe.db.get_value("Plan", new_plan, "sort_order") or 0
    return "upgrade" if n > o else "downgrade"


def disk_shrinks(old_plan: str, new_plan: str) -> bool:
    """True when the target plan implies a smaller disk (limits.disk_gb); FC refuses disk shrink."""
    o, n = _limits(old_plan).get("disk_gb"), _limits(new_plan).get("disk_gb")
    return o is not None and n is not None and float(n) < float(o)


def can_change_plan(user: str, tenant: str, new_plan: str) -> tuple[bool, str]:
    """Return (allowed, reason) for moving `tenant` to `new_plan`. Does not consult the tenant cap."""
    if not new_plan or not frappe.db.exists("Plan", new_plan):
        return False, "plan_not_found"
    if not frappe.db.get_value("Plan", new_plan, "available") or not frappe.db.get_value("Plan", new_plan, "is_public"):
        return False, "plan_unavailable"
    t = frappe.db.get_value("Tenant", tenant, ["owner_user", "status", "plan"], as_dict=True) if frappe.db.exists("Tenant", tenant) else None
    if not t:
        return False, "tenant_not_found"
    if t.get("owner_user") != user:
        return False, "not_owner"
    if t.get("status") != "active":
        return False, "tenant_not_active"
    if t.get("plan") == new_plan:
        return False, "same_plan"
    return True, ""
