"""App install + DocType creation/naming (bench only)."""
import frappe
from frappe.tests.utils import FrappeTestCase

from saas_core.tests import _bench_common as c


class BenchInstall(FrappeTestCase):
    def test_roles_exist(self):
        for role in ("SaaS Admin", "SaaS Customer"):
            self.assertTrue(frappe.db.exists("Role", role), role)

    def test_doctypes_installed(self):
        for dt in ("Plan", "Tenant", "Subscription", "Provision Job", "Payment Event", "Gateway Account"):
            self.assertTrue(frappe.db.exists("DocType", dt), dt)

    def test_plan_naming(self):
        plan, _ = c.seed_catalog("bi")
        self.assertEqual(frappe.get_doc("Plan", plan).name, plan)  # autoname field:code

    def test_tenant_naming_by_slug(self):
        plan, tpl = c.seed_catalog("bi")
        t = c.make_tenant("bi-acme", "Administrator", plan, tpl)
        self.assertEqual(t.name, "bi-acme")

    def test_subscription_series(self):
        plan, tpl = c.seed_catalog("bi")
        c.make_tenant("bi-sub", "Administrator", plan, tpl)
        s = frappe.get_doc({"doctype": "Subscription", "tenant": "bi-sub", "owner_user": "Administrator",
                            "plan": plan, "status": "active"}).insert(ignore_permissions=True)
        self.assertTrue(s.name.startswith("SUB-"), s.name)

    def test_provision_job_series(self):
        plan, tpl = c.seed_catalog("bi")
        c.make_tenant("bi-job", "Administrator", plan, tpl)
        j = frappe.get_doc({"doctype": "Provision Job", "tenant": "bi-job", "kind": "provision",
                            "state": "pending"}).insert(ignore_permissions=True)
        self.assertTrue(j.name.startswith("PJ-"), j.name)
