"""API permission checks: owner isolation (404), guest rejection, admin sees all (bench only)."""
import frappe
from frappe.tests.utils import FrappeTestCase

from saas_core.tests import _bench_common as c

CUSTOMER = "SaaS Customer"
ADMIN = "SaaS Admin"


class BenchApiPermissions(FrappeTestCase):
    def setUp(self):
        self.plan, self.tpl = c.seed_catalog("bp")
        self.a = c.ensure_user("bp-a@example.test", (CUSTOMER,))
        self.b = c.ensure_user("bp-b@example.test", (CUSTOMER,))
        self.admin = c.ensure_user("bp-admin@example.test", (ADMIN,))
        c.make_tenant("bp-ta", self.a, self.plan, self.tpl)
        c.make_tenant("bp-tb", self.b, self.plan, self.tpl)

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_owner_sees_own_tenant(self):
        from saas_core.api.v1 import tenants
        frappe.set_user(self.a)
        self.assertEqual(tenants.get(tenant="bp-ta")["slug"], "bp-ta")

    def test_customer_cannot_see_other_tenant(self):
        from saas_core.api.v1 import tenants
        from saas_core.api.v1._common import ApiError
        frappe.set_user(self.a)
        with self.assertRaises(ApiError) as cm:
            tenants.get(tenant="bp-tb")
        self.assertEqual(getattr(cm.exception, "http_status", 404), 404)

    def test_list_only_own(self):
        from saas_core.api.v1 import tenants
        frappe.set_user(self.a)
        slugs = {t["slug"] for t in tenants.list()["items"]}
        self.assertIn("bp-ta", slugs)
        self.assertNotIn("bp-tb", slugs)

    def test_guest_rejected_on_auth_endpoints(self):
        from saas_core.api.v1 import tenants
        from saas_core.api.v1.auth import me
        frappe.set_user("Guest")
        with self.assertRaises(Exception):
            tenants.get(tenant="bp-ta")
        with self.assertRaises(Exception):
            me()

    def test_admin_sees_all(self):
        from saas_core import permissions
        for t in ("bp-ta", "bp-tb"):
            self.assertEqual(permissions.assert_tenant_access(t, self.admin).name, t)
