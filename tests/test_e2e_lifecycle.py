"""Pure-pytest end-to-end lifecycle on the fake frappe + FakeFcctl (no bench, no network).

signup disabled -> admin creates tenant -> manual mock checkout (payment_mocked) -> provision job runs the
restore-from-golden bucket flow -> active + login link -> suspend / resume / change plan / backup /
delete with two-person approval -> killed-worker resume with no duplicate server.
The same flow on a real bench is saas_core/tests/test_bench_lifecycle.py.
"""
import hashlib
import json

import pytest

PARTS = {"database.sql.gz": b"db-bytes", "public.tar": b"pub-bytes", "private.tar": b"priv-bytes"}
URLS = dict(success_url="https://cp.test/ok", cancel_url="https://cp.test/no")


def mk(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


def golden_source():
    return {"bucket": "acme-golden", "prefix": "acme/v1",
            "manifest": {"parts": [{"name": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in PARTS.items()]}}


@pytest.fixture
def world(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    frappe.conf["saas_core_site_domain"] = "apps.test"
    frappe._Doc.get_password = lambda self, k, raise_exception=True: self.get(k)  # fresh stub per test
    fake = FakeFcctl()
    for n, b in PARTS.items():
        fake.bucket_put("acme-golden", f"acme/v1/{n}", b)
    runner.set_port(fake)
    mk(frappe, "Template", name="acme", slug="acme",
       apps=[{"app": "frappe", "install_order": 1}, {"app": "acme", "install_order": 2}],
       backup_source=json.dumps(golden_source()))
    for i, (name, disk) in enumerate((("acme-standard", 50), ("acme-growth", 100))):
        mk(frappe, "Plan", name=name, code=name, template="acme", available=1, is_public=1, sort_order=i, fc_server_plan=f"fc-{name}",
           limits=json.dumps({"disk_gb": disk}),
           prices=[{"interval": "month", "currency": "USD", "amount": 4900, "gateway": "manual"}])
    mk(frappe, "Gateway Account", account_name="manual", gateway="manual", mode="test", enabled=1)
    yield frappe, runner, fake
    runner.set_port(None)


def as_user(frappe, user, *roles):
    frappe.session.user = user
    frappe._roles = list(roles)


def run_pending(frappe, runner, fake, kind=None):
    """Run every pending job (what the RQ worker does after commit). Returns {job: state}."""
    out = {}
    for j in frappe.get_all("Provision Job", filters={"state": "pending"}):
        if kind is None or j.kind == kind:
            out[j.name] = runner.run_job(j.name, port=fake)
    return out


def test_full_lifecycle(world):
    frappe, runner, fake = world
    from saas_core import jobs
    from saas_core.api.v1 import auth, checkout
    from saas_core.api.v1._common import ApiError
    from saas_core.api.v1.admin import tenants as admin
    from saas_core.api.v1 import tenants as t
    from saas_core.provisioning.stores import FrappeApprovalStore

    # 1. public signup disabled
    as_user(frappe, "Guest")
    frappe.conf["saas_core_public_signup"] = 0
    out = auth.signup(email="x@y.co", password="longpass123")
    assert out["error"]["code"] == "signup_disabled" and frappe.local.response["http_status_code"] == 403
    cfg = auth.config()
    assert cfg["public_signup"] is False and cfg["payment_banner"]

    # 2. admin creates the tenant
    as_user(frappe, "admin@cp.test", "SaaS Admin")
    created = admin.create(owner_email="owner@acme.co", slug="acme", plan="acme-standard")
    assert created["tenant"]["status"] == "pending" and created["owner_created"]
    assert frappe.get_doc("Tenant", "acme").template == "acme"
    prov_job = created["job"]

    # 3. owner pays with the manual (mock) gateway
    as_user(frappe, "owner@acme.co", "SaaS Customer")
    # P1-2: admin.create already attached a comped (live) subscription, so a second checkout is refused ...
    refused = checkout.create_session(plan="acme-standard", interval="month", tenant="acme", gateway="manual", **URLS)
    assert refused["error"]["code"] == "conflict"
    # ... drop it to model an unpaid tenant and keep exercising the manual mock checkout.
    for s_ in frappe.get_all("Subscription", filters={"tenant": "acme"}):
        s_.delete()
    co = checkout.create_session(plan="acme-standard", interval="month", tenant="acme", gateway="manual", **URLS)
    assert co["payment_mocked"] is True and co["subscription_status"] == "active"
    sub = frappe.get_all("Subscription", filters={"tenant": "acme"})[0]
    assert sub.status == "active" and sub.payment_mocked == 1
    assert all(i.payment_mocked == 1 and i.status == "paid" for i in frappe.get_all("Invoice"))
    assert len(frappe.get_all("Provision Job", filters={"kind": "provision"})) == 1   # deduped

    # 4. provisioning job: restore-from-golden through the bucket source
    assert runner.run_job(prov_job, port=fake) == "succeeded"
    tenant = frappe.get_doc("Tenant", "acme")
    assert tenant.status == "active" and tenant.fc_site == "acme.apps.test"
    assert fake.sites_created == 1
    assert fake.count("bucket_get") == 3 and fake.count("create_upload") == 1
    assert len(fake.restores) == 1 and sorted(fake.restores[0]["parts"]) == sorted(PARTS)
    assert fake.uploads[fake.restores[0]["upload_id"]]["parts"] == PARTS

    # 5. login link
    link = t.open_login_link(tenant="acme")
    assert link["url"].startswith("https://acme.apps.test/")

    # 6. suspend / resume
    jobs.enqueue_job("acme", "suspend", args={"reason": "manual"})
    assert list(run_pending(frappe, runner, fake).values()) == ["succeeded"]
    assert frappe.get_doc("Tenant", "acme").status == "suspended" and fake.sites["acme.apps.test"]["status"] == "suspended"
    with pytest.raises(ApiError) as e:
        t.open_login_link(tenant="acme")
    assert e.value.http_status == 409
    jobs.enqueue_job("acme", "resume")
    assert list(run_pending(frappe, runner, fake).values()) == ["succeeded"]
    assert frappe.get_doc("Tenant", "acme").status == "active" and fake.sites["acme.apps.test"]["status"] == "active"

    # 7. change plan (upgrade, bigger disk)
    r = t.change_plan(tenant="acme", plan="acme-growth")
    assert r["kind"] == "change_plan"
    assert list(run_pending(frappe, runner, fake).values()) == ["succeeded"]
    assert frappe.get_doc("Tenant", "acme").plan == "acme-growth"
    assert fake.sites["acme.apps.test"]["plan"] == "fc-acme-growth"
    with pytest.raises(ApiError) as e:
        t.change_plan(tenant="acme", plan="acme-standard")
    assert e.value.field_errors == {"plan": "disk_shrink_not_supported"}

    # 8. backup
    jobs.enqueue_job("acme", "backup")
    assert list(run_pending(frappe, runner, fake).values()) == ["succeeded"]
    assert len(fake.list_backups("acme.apps.test")) == 1

    # 9. delete needs two-person approval
    d = t.request_delete(tenant="acme", confirm_slug="acme", reason="done")
    job = frappe.get_doc("Provision Job", d["name"])
    assert job.kind == "terminate" and job.approval
    assert runner.run_job(job.name, port=fake) == "needs_attention"             # unapproved: refused
    assert fake.count("terminate_site") == 0 and "acme.apps.test" in fake.sites
    ap = FrappeApprovalStore(roles_for=lambda u: ["SaaS Admin"])
    ap.approve(job.approval, "admin1@cp.test")
    ap.approve(job.approval, "admin2@cp.test")
    job.state = "pending"
    job.save()
    assert runner.run_job(job.name, port=fake) == "succeeded"
    assert fake.count("terminate_site") == 1 and "acme.apps.test" not in fake.sites
    assert frappe.get_doc("Approval Request", job.approval).status == "executed"
    assert frappe.get_doc("Tenant", "acme").status == "terminating"


def test_killed_worker_resume_no_duplicate_server(world):
    frappe, runner, fake = world
    from saas_core.api.v1.admin import tenants as admin
    from saas_core.provisioning.fake_fcctl import FakeCrash
    as_user(frappe, "admin@cp.test", "SaaS Admin")
    job = admin.create(owner_email="o@acme.co", slug="acme", plan="acme-standard")["job"]
    fake.crash_after = "create_site"
    with pytest.raises(FakeCrash):
        runner.run_job(job, port=fake)
    assert frappe.get_doc("Provision Job", job).state == "dispatching" and fake.sites_created == 1
    assert runner.run_job(job, port=fake) == "succeeded"
    assert fake.sites_created == 1 and len(fake.sites) == 1 and len(fake.restores) == 1
    assert frappe.get_doc("Tenant", "acme").status == "active"


def test_killed_during_restore_resumes_restore(world):
    frappe, runner, fake = world
    from saas_core.api.v1.admin import tenants as admin
    from saas_core.provisioning.fake_fcctl import FakeCrash
    as_user(frappe, "admin@cp.test", "SaaS Admin")
    job = admin.create(owner_email="o@acme.co", slug="acme", plan="acme-standard")["job"]
    fake.crash_after = "restore_backup"
    with pytest.raises(FakeCrash):
        runner.run_job(job, port=fake)
    assert fake.sites_created == 1 and len(fake.restores) == 1
    assert runner.run_job(job, port=fake) == "succeeded"
    assert fake.sites_created == 1 and frappe.get_doc("Tenant", "acme").status == "active"


def test_paused_fc_session_leaves_job_pending(world):
    frappe, runner, fake = world
    from saas_core import jobs
    from saas_core.api.v1.admin import tenants as admin
    as_user(frappe, "admin@cp.test", "SaaS Admin")
    job = admin.create(owner_email="o@acme.co", slug="acme", plan="acme-standard")["job"]
    jobs.fc_pause("test")
    assert runner.run_job(job, port=fake) == "pending"
    assert fake.sites_created == 0 and frappe.get_doc("Provision Job", job).attempts == 0
    jobs.fc_unpause()
    assert runner.run_job(job, port=fake) == "succeeded"
