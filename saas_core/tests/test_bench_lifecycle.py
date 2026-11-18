"""Full tenant lifecycle on a real bench with FakeFcctl (site_config saas_core_fake_fcctl=1 on the test site).

Mirrors tests/test_e2e_lifecycle.py against the real DB, doctypes, permissions and API wrappers:
signup disabled (403) -> admin creates tenant -> manual mock checkout (payment_mocked=1) -> provision job runs
the restore-from-golden bucket flow -> active + login link -> suspend / resume / change plan / backup ->
delete with two-person approval -> killed-worker resume with no duplicate server.

Needs: bench migrate (Platform Settings, Custom Fields), `bench --site <s> set-config allow_tests true`.
Not run in CI by the pytest suite; run with scripts/bench_test.sh.
"""
import hashlib
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from saas_core.tests import _bench_common as c

PARTS = {"database.sql.gz": b"db-bytes", "public.tar": b"pub-bytes", "private.tar": b"priv-bytes"}
P = "bl"
URLS = {"success_url": "https://cp.test/ok", "cancel_url": "https://cp.test/no"}


def golden_source():
    return {"bucket": "acme-golden", "prefix": "acme/v1",
            "manifest": {"parts": [{"name": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in PARTS.items()]}}


class BenchLifecycle(FrappeTestCase):
    def setUp(self):
        from saas_core.tests._rl import clear_rate_limits
        clear_rate_limits()
        from saas_core.provisioning import runner
        from saas_core.provisioning.fake_fcctl import FakeFcctl
        frappe.set_user("Administrator")
        frappe.conf["saas_core_site_domain"] = "apps.test"
        frappe.conf["saas_core_fake_fcctl"] = 1
        frappe.conf["saas_core_public_signup"] = 0
        self.fake = FakeFcctl()
        for n, b in PARTS.items():
            self.fake.bucket_put("acme-golden", f"acme/v1/{n}", b)
        runner.set_port(self.fake)
        self.admin = c.ensure_user(f"{P}-admin@cp.test", ("SaaS Admin",))
        self.admin2 = c.ensure_user(f"{P}-admin2@cp.test", ("SaaS Admin",))
        self.admin3 = c.ensure_user(f"{P}-admin3@cp.test", ("SaaS Admin",))
        self._purge()
        self._seed()

    def _purge(self):
        """Admin create commits, so a prior run leaves bl-* tenants behind ('subdomain already taken')."""
        apr = frappe.get_all("Provision Job", filters={"tenant": ["like", f"{P}-%"]}, pluck="approval")
        for dt in ("Provision Job", "Subscription", "Invoice", "API Token", "Tenant Member", "Support Ticket"):
            frappe.db.delete(dt, {"tenant": ["like", f"{P}-%"]})
        for a in filter(None, apr):
            frappe.db.delete("Approval Request", {"name": a})
        frappe.db.delete("Tenant", {"name": ["like", f"{P}-%"]})
        frappe.db.commit()

    def tearDown(self):
        self._purge()
        from saas_core.provisioning import runner
        runner.set_port(None)
        frappe.set_user("Administrator")
        frappe.conf.pop("saas_core_public_signup", None)

    def _seed(self):
        if not frappe.db.exists("Template", f"{P}-acme"):
            frappe.get_doc({"doctype": "Template", "slug": f"{P}-acme", "title": "Acme",
                            "apps": [{"app": "frappe", "install_order": 1}, {"app": "acme", "install_order": 2}],
                            "hooks": json.dumps([]), "site_config": json.dumps({}),
                            "backup_source": json.dumps(golden_source())}).insert(ignore_permissions=True)
        for i, (code, disk) in enumerate(((f"{P}-standard", 50), (f"{P}-growth", 100))):
            if not frappe.db.exists("Plan", code):
                frappe.get_doc({"doctype": "Plan", "code": code, "title": code, "template": f"{P}-acme",
                                "fc_server_plan": f"fc-{code}", "is_public": 1, "available": 1, "sort_order": i + 100,
                                "limits": json.dumps({"disk_gb": disk}),
                                "prices": [{"interval": "month", "currency": "USD", "amount": 4900,
                                            "gateway": "manual"}]}).insert(ignore_permissions=True)
        if not frappe.db.exists("Gateway Account", f"{P}-manual"):
            frappe.get_doc({"doctype": "Gateway Account", "account_name": f"{P}-manual", "gateway": "manual",
                            "mode": "test", "enabled": 1}).insert(ignore_permissions=True)

    def _run_pending(self, tenant, kind):
        from saas_core.provisioning import runner
        names = frappe.get_all("Provision Job", filters={"tenant": tenant, "kind": kind, "state": "pending"}, pluck="name")
        self.assertEqual(len(names), 1, f"expected one pending {kind} job, got {names}")
        return runner.run_job(names[0], port=self.fake)

    def test_full_lifecycle(self):
        from saas_core import jobs
        from saas_core.api.v1 import auth, checkout
        from saas_core.api.v1 import tenants as t
        from saas_core.api.v1.admin import tenants as admin
        from saas_core.provisioning import runner
        from saas_core.provisioning.stores import FrappeApprovalStore
        slug, plan, grow = f"{P}-acme", f"{P}-standard", f"{P}-growth"

        # signup disabled
        frappe.set_user("Guest")
        out = auth.signup(email=f"{P}-x@y.co", password="a-long-password-123")
        self.assertEqual(out["error"]["code"], "signup_disabled")
        self.assertFalse(auth.config()["public_signup"])

        # admin creates tenant
        frappe.set_user(self.admin)
        owner = f"{P}-owner@acme.co"
        created = admin.create(owner_email=owner, slug=slug, plan=plan)
        self.assertEqual(created["tenant"]["status"], "pending")
        prov = created["job"]

        # owner pays with the mock gateway. Post-T5: the client cannot pick the gateway (default must be
        # manual here) and admin.create comps a live subscription, so drop it to model an unpaid tenant.
        frappe.local.conf["saas_core_default_gateway"] = "manual"
        for sname in frappe.get_all("Subscription", filters={"tenant": slug}, pluck="name"):
            frappe.delete_doc("Subscription", sname, ignore_permissions=True, force=True)
        frappe.set_user(owner)
        co = checkout.create_session(plan=plan, interval="month", tenant=slug, gateway="manual", **URLS)
        self.assertTrue(co["payment_mocked"])
        self.assertEqual(frappe.db.get_value("Subscription", {"tenant": slug}, "payment_mocked"), 1)

        # provisioning via the bucket restore-from-golden flow
        frappe.set_user("Administrator")
        self.assertEqual(runner.run_job(prov, port=self.fake), "succeeded")
        self.assertEqual(frappe.db.get_value("Tenant", slug, "status"), "active")
        site = frappe.db.get_value("Tenant", slug, "fc_site")
        self.assertEqual(self.fake.sites_created, 1)
        self.assertEqual(len(self.fake.restores), 1)

        # login link
        frappe.set_user(owner)
        self.assertTrue(t.open_login_link(tenant=slug)["url"].startswith(f"https://{site}/"))

        # suspend / resume
        frappe.set_user("Administrator")
        jobs.enqueue_job(slug, "suspend", args={"reason": "manual"})
        self.assertEqual(self._run_pending(slug, "suspend"), "succeeded")
        self.assertEqual(frappe.db.get_value("Tenant", slug, "status"), "suspended")
        jobs.enqueue_job(slug, "resume")
        self.assertEqual(self._run_pending(slug, "resume"), "succeeded")
        self.assertEqual(frappe.db.get_value("Tenant", slug, "status"), "active")

        # change plan
        frappe.set_user(owner)
        t.change_plan(tenant=slug, plan=grow)
        frappe.set_user("Administrator")
        self.assertEqual(self._run_pending(slug, "change_plan"), "succeeded")
        self.assertEqual(frappe.db.get_value("Tenant", slug, "plan"), grow)

        # backup
        jobs.enqueue_job(slug, "backup")
        self.assertEqual(self._run_pending(slug, "backup"), "succeeded")
        self.assertEqual(len(self.fake.list_backups(site)), 1)

        # delete needs two approvals
        frappe.set_user(owner)
        d = t.request_delete(tenant=slug, confirm_slug=slug)
        frappe.set_user("Administrator")
        job = frappe.get_doc("Provision Job", d["name"])
        self.assertEqual(runner.run_job(job.name, port=self.fake), "needs_attention")
        self.assertEqual(self.fake.count("terminate_site"), 0)
        ap = FrappeApprovalStore()
        ap.approve(job.approval, self.admin)
        ap.approve(job.approval, self.admin2)
        frappe.db.set_value("Provision Job", job.name, "state", "pending")
        self.assertEqual(runner.run_job(job.name, port=self.fake), "succeeded")
        self.assertEqual(self.fake.count("terminate_site"), 1)
        self.assertEqual(frappe.db.get_value("Approval Request", job.approval, "status"), "executed")

    def test_killed_worker_resume_no_duplicate_server(self):
        from saas_core.api.v1.admin import tenants as admin
        from saas_core.provisioning import runner
        from saas_core.provisioning.fake_fcctl import FakeCrash
        frappe.set_user(self.admin)
        slug = f"{P}-crash"
        job = admin.create(owner_email=f"{P}-o2@acme.co", slug=slug, plan=f"{P}-standard")["job"]
        frappe.set_user("Administrator")
        self.fake.crash_after = "create_site"
        with self.assertRaises(FakeCrash):
            runner.run_job(job, port=self.fake)
        self.assertEqual(runner.run_job(job, port=self.fake), "succeeded")
        self.assertEqual(self.fake.sites_created, 1)
        self.assertEqual(len(self.fake.sites), 1)
        self.assertEqual(frappe.db.get_value("Tenant", slug, "status"), "active")

    def test_paused_fc_session_leaves_job_pending(self):
        from saas_core import jobs
        from saas_core.api.v1.admin import tenants as admin
        from saas_core.provisioning import runner
        frappe.set_user(self.admin)
        job = admin.create(owner_email=f"{P}-o3@acme.co", slug=f"{P}-paused", plan=f"{P}-standard")["job"]
        frappe.set_user("Administrator")
        jobs.fc_pause("bench test")
        try:
            self.assertEqual(runner.run_job(job, port=self.fake), "pending")
            self.assertEqual(self.fake.sites_created, 0)
        finally:
            jobs.fc_unpause()
