"""Shared builders for provisioning tests (no saas_core imports at module level)."""
import json


def seed(frappe, slug="acme", recipe=None, status="pending", fc_site=None):
    frappe.conf["saas_core_site_domain"] = "apps.test"
    frappe.get_doc({"doctype": "Plan", "name": "pro", "code": "pro", "fc_server_plan": "n1-standard"}).insert()
    frappe.get_doc({"doctype": "Template", "name": "erp", "slug": "erp", "recipe": recipe,
                    "apps": [{"app": "frappe", "install_order": 1}, {"app": "erpnext", "install_order": 2}],
                    "hooks": json.dumps([{"name": "core.clear_cache", "version": 1}]),
                    "site_config": json.dumps({"x": 1})}).insert()
    frappe.get_doc({"doctype": "Tenant", "name": slug, "slug": slug, "status": status, "plan": "pro",
                    "template": "erp", "fc_site": fc_site, "recipe_fields": json.dumps({"co": "Acme"})}).insert()
    return slug


def job(frappe, tenant="acme", kind="provision", **kw):
    d = {"doctype": "Provision Job", "tenant": tenant, "kind": kind, "state": "pending", "attempts": 0, "args": "{}"}
    d.update(kw)
    return frappe.get_doc(d).insert().name


def tenant_status(frappe, t="acme"):
    return frappe.get_doc("Tenant", t).get("status")
