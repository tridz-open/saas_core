"""Provisioning job end-to-end with FakeFcctl injected via the runner port (bench only)."""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from saas_core.tests import _bench_common as c


class BenchProvisioning(FrappeTestCase):
    def setUp(self):
        from saas_core.provisioning import runner
        from saas_core.provisioning.fake_fcctl import FakeFcctl
        frappe.set_user("Administrator")
        frappe.conf["saas_core_site_domain"] = "apps.test"
        self.fake = FakeFcctl()
        runner.set_port(self.fake)

    def tearDown(self):
        from saas_core.provisioning import runner
        runner.set_port(None)

    def _job(self, slug):
        plan, tpl = c.seed_catalog("pv")
        c.make_tenant(slug, "Administrator", plan, tpl)
        return frappe.get_doc({"doctype": "Provision Job", "tenant": slug, "kind": "provision", "state": "pending",
                               "attempts": 0, "args": json.dumps({})}).insert(ignore_permissions=True).name

    def test_provision_end_to_end(self):
        from saas_core.provisioning import runner
        job = self._job("pv-ok")
        state = runner.run_job(job, port=self.fake)
        self.assertEqual(state, "succeeded")
        self.assertEqual(self.fake.count("provision_site"), 1)
        self.assertEqual(frappe.db.get_value("Tenant", "pv-ok", "status"), "active")
        self.assertTrue(frappe.db.get_value("Tenant", "pv-ok", "fc_site"))

    def test_rerun_of_terminal_job_is_noop(self):
        from saas_core.provisioning import runner
        job = self._job("pv-again")
        runner.run_job(job, port=self.fake)
        self.assertEqual(runner.run_job(job, port=self.fake), "succeeded")
        self.assertEqual(self.fake.count("provision_site"), 1)

    def test_failure_injection(self):
        from saas_core.provisioning import runner
        from saas_core.provisioning.fake_fcctl import FakeFcctl
        bad = FakeFcctl(fail_at_step="install_apps")
        runner.set_port(bad)
        job = self._job("pv-fail")
        state = runner.run_job(job, port=bad)
        self.assertNotEqual(state, "succeeded")
        self.assertNotEqual(frappe.db.get_value("Tenant", "pv-fail", "status"), "active")
