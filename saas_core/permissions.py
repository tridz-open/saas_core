"""Row-level permissions for SaaS Core: customers see only their own tenants and tenant-owned rows."""
from __future__ import annotations

import frappe

PRIVILEGED_ROLES = frozenset({"SaaS Admin", "System Manager"})


def is_privileged(user: str | None = None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool(PRIVILEGED_ROLES & set(frappe.get_roles(user)))


def _owner_clause(table: str, user: str) -> str:
    return f"`tab{table}`.`owner_user` = {frappe.db.escape(user)}"


def _tenant_scoped(table: str, user: str) -> str:
    return (
        f"`tab{table}`.`tenant` in (select `name` from `tabTenant` "
        f"where `owner_user` = {frappe.db.escape(user)})"
    )


def tenant_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    return "" if is_privileged(user) else _owner_clause("Tenant", user)


def subscription_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    return "" if is_privileged(user) else _tenant_scoped("Subscription", user)


def invoice_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    return "" if is_privileged(user) else _tenant_scoped("Invoice", user)


def job_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    return "" if is_privileged(user) else _tenant_scoped("Provision Job", user)


def ticket_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    return "" if is_privileged(user) else _owner_clause("Support Ticket", user)


def has_tenant_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    user = user or frappe.session.user
    if is_privileged(user):
        return True
    return bool(user) and user != "Guest" and doc.get("owner_user") == user


def has_tenant_child_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    """For docs carrying a `tenant` link (Subscription, Invoice, Provision Job)."""
    user = user or frappe.session.user
    if is_privileged(user):
        return True
    tenant = doc.get("tenant")
    if not tenant or not user or user == "Guest":
        return False
    return frappe.db.get_value("Tenant", tenant, "owner_user") == user


def has_ticket_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    user = user or frappe.session.user
    if is_privileged(user):
        return True
    return bool(user) and user != "Guest" and doc.get("owner_user") == user


def assert_tenant_access(tenant_name: str, user: str | None = None, ptype: str = "read"):
    """Return the Tenant doc, or raise DoesNotExistError (404) so existence is not leaked."""
    user = user or frappe.session.user
    if not frappe.db.exists("Tenant", tenant_name):
        raise frappe.DoesNotExistError(f"Tenant {tenant_name} not found")
    doc = frappe.get_doc("Tenant", tenant_name)
    if not has_tenant_permission(doc, ptype, user):
        raise frappe.DoesNotExistError(f"Tenant {tenant_name} not found")
    return doc
