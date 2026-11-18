"""Shared helpers for bench-only tests (not collected by pytest; run via `bench run-tests --app saas_core`)."""
import json

import frappe


def ensure_user(email: str, roles: tuple = ()) -> str:
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
                        "send_welcome_email": 0, "enabled": 1}).insert(ignore_permissions=True)
    u = frappe.get_doc("User", email)
    for r in roles:
        if r not in [x.role for x in u.roles]:
            u.append("roles", {"role": r})
    u.save(ignore_permissions=True)
    return email


def seed_catalog(slug_prefix: str = "bt"):
    """Idempotently create a Plan and Template; return (plan, template)."""
    if not frappe.db.exists("Plan", f"{slug_prefix}-plan"):
        frappe.get_doc({"doctype": "Plan", "code": f"{slug_prefix}-plan", "title": "Bench Plan",
                        "fc_server_plan": "n1-standard", "is_public": 1, "available": 1}).insert(ignore_permissions=True)
    if not frappe.db.exists("Template", f"{slug_prefix}-tpl"):
        frappe.get_doc({"doctype": "Template", "slug": f"{slug_prefix}-tpl", "title": "Bench Template",
                        "apps": [{"app": "frappe", "install_order": 1}],
                        "hooks": json.dumps([]), "site_config": json.dumps({})}).insert(ignore_permissions=True)
    return f"{slug_prefix}-plan", f"{slug_prefix}-tpl"


def make_tenant(slug: str, owner: str, plan: str, template: str, status: str = "pending"):
    if frappe.db.exists("Tenant", slug):
        return frappe.get_doc("Tenant", slug)
    return frappe.get_doc({"doctype": "Tenant", "slug": slug, "owner_user": owner, "plan": plan,
                           "template": template, "status": status}).insert(ignore_permissions=True)
