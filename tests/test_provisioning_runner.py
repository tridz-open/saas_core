import pytest
from prov_helpers import seed, job, tenant_status


def mods():
    from saas_core.provisioning import runner, reconcile
    from saas_core.provisioning.fake_fcctl import FakeFcctl, FakeCrash
    return runner, reconcile, FakeFcctl, FakeCrash


def test_happy_path(frappe):
    runner, _, Fake, _ = mods()
    seed(frappe); j = job(frappe); port = Fake()
    assert runner.run_job(j, port=port) == "succeeded"
    t = frappe.get_doc("Tenant", "acme")
    assert t.status == "active" and t.fc_site == "acme.apps.test" and t.workflow_id
    assert port.sites_created == 1
    steps = frappe.get_doc("Provision Job", j).steps
    assert steps and steps[-1]["state"] == "succeeded"
    assert any(e[1]["type"] == "tenant.active" for e in frappe._store.realtime)
    assert runner.run_job(j, port=port) == "succeeded" and port.count("provision_site") == 1


def test_killed_worker_resume_no_duplicate_site(frappe):
    runner, _, Fake, Crash = mods()
    seed(frappe); j = job(frappe)
    port = Fake(crash_after="create_site")
    with pytest.raises(Crash):
        runner.run_job(j, port=port)
    assert frappe.get_doc("Provision Job", j).state == "dispatching"
    assert port.sites_created == 1
    assert runner.run_job(j, port=port) == "succeeded"
    assert port.sites_created == 1 and len(port.sites) == 1
    assert tenant_status(frappe) == "active"
    wf = frappe.get_doc("Provision Job", j).workflow_id
    assert port.workflows[wf]["state"] == "succeeded"


def test_crash_after_completion_adopts_without_reprovision(frappe):
    runner, _, Fake, Crash = mods()
    seed(frappe); j = job(frappe); port = Fake(crash_after="provision_site")
    with pytest.raises(Crash):
        runner.run_job(j, port=port)
    assert runner.run_job(j, port=port) == "succeeded"
    assert port.count("provision_site") == 1 and port.sites_created == 1


def test_failure_needs_attention(frappe):
    runner, _, Fake, _ = mods()
    seed(frappe); j = job(frappe); port = Fake(fail_at_step="install_apps")
    assert runner.run_job(j, port=port) == "needs_attention"
    d = frappe.get_doc("Provision Job", j)
    assert d.needs_attention == 1 and "install_apps" in d.error
    assert tenant_status(frappe) == "failed"


def test_unexpected_exception_is_outcome_unknown_then_reconciled(frappe):
    runner, reconcile, Fake, _ = mods()
    seed(frappe); j = job(frappe)
    port = Fake()
    orig = port.provision_site
    def boom(spec):
        orig(spec); raise TimeoutError("net")
    port.provision_site = boom
    assert runner.run_job(j, port=port) == "outcome_unknown"
    port.provision_site = orig
    out = reconcile.run(port=port)
    assert out[j] == "adopted"
    assert frappe.get_doc("Provision Job", j).state == "succeeded" and tenant_status(frappe) == "active"
    assert port.sites_created == 1


def test_reconcile_redispatches_when_port_never_saw_it(frappe):
    runner, reconcile, Fake, _ = mods()
    seed(frappe); j = job(frappe, state="outcome_unknown", workflow_id="wf-x")
    out = reconcile.run(port=Fake())
    assert out[j] == "redispatched"
    assert frappe._store.enqueued[-1][1]["job"] == j


def test_reconcile_terminate_unknown_needs_attention(frappe):
    _, reconcile, Fake, _ = mods()
    seed(frappe, fc_site="acme.apps.test"); j = job(frappe, kind="terminate", state="outcome_unknown")
    assert reconcile.run(port=Fake())[j] == "needs_attention"


def test_suspend_resume_change_plan_backup(frappe):
    runner, _, Fake, _ = mods()
    seed(frappe, status="active", fc_site="acme.apps.test"); port = Fake()
    port.sites["acme.apps.test"] = {"status": "active"}
    runner.run_job(job(frappe, kind="suspend", args='{"reason": "dunning"}'), port=port)
    t = frappe.get_doc("Tenant", "acme")
    assert t.status == "suspended" and t.suspend_reason == "dunning" and port.sites["acme.apps.test"]["status"] == "suspended"
    runner.run_job(job(frappe, kind="resume"), port=port)
    assert tenant_status(frappe) == "active"
    runner.run_job(job(frappe, kind="change_plan", args='{"plan": "pro"}'), port=port)
    assert frappe.get_doc("Tenant", "acme").plan == "pro" and port.sites["acme.apps.test"]["plan"] == "n1-standard"
    jb = job(frappe, kind="backup")
    assert runner.run_job(jb, port=port) == "succeeded" and port.list_backups("acme.apps.test")


def test_terminate_requires_two_person_approval(frappe):
    runner, _, Fake, _ = mods()
    from saas_core.provisioning.stores import FrappeApprovalStore
    seed(frappe, status="active", fc_site="acme.apps.test"); port = Fake()
    port.sites["acme.apps.test"] = {"status": "active"}
    j0 = job(frappe, kind="terminate")                       # no approval
    assert runner.run_job(j0, port=port) == "needs_attention" and not port.count("terminate_site")
    ap = FrappeApprovalStore(roles_for=lambda u: ["SaaS Admin"])
    a = ap.request("terminate", "acme", None, "alice")
    ap.approve(a, "bob")                                     # only one approver
    j1 = job(frappe, kind="terminate", approval=a)
    assert runner.run_job(j1, port=port) == "needs_attention" and not port.count("terminate_site")
    ap.approve(a, "carol")
    j2 = job(frappe, kind="terminate", approval=a)
    assert runner.run_job(j2, port=port) == "succeeded"
    assert "acme.apps.test" not in port.sites
    assert frappe.get_doc("Approval Request", a).status == "executed"
    assert tenant_status(frappe) == "terminating"


def test_recipe_tenant_config_and_get_port(frappe):
    runner, _, Fake, _ = mods()
    from saas_core.recipes import registry
    from saas_core.recipes.base import Recipe
    class R(Recipe):
        slug = "r"
        def tenant_config(self, tenant, plan, fields): return {"company": fields["co"]}
    registry.register(R())
    seed(frappe, recipe="r"); port = Fake()
    runner.run_job(job(frappe), port=port)
    assert port.sites["acme.apps.test"]["config"] == {"x": 1, "company": "Acme"}
    frappe.conf["saas_core_fake_fcctl"] = 1
    runner.set_port(None)
    assert isinstance(runner.get_port(), Fake.__mro__[0])
    runner.set_port(None)


def test_effective_at_future_defers_then_runs(frappe):
    import datetime as dt
    runner, _, Fake, _ = mods()
    seed(frappe, status="active", fc_site="acme.apps.test"); port = Fake()
    port.sites["acme.apps.test"] = {"status": "active"}
    future = (dt.datetime.utcnow() + dt.timedelta(days=3)).isoformat()
    past = (dt.datetime.utcnow() - dt.timedelta(days=1)).isoformat()
    j = job(frappe, kind="change_plan", args='{"plan": "pro", "effective_at": "%s"}' % future)
    assert runner.run_job(j, port=port) == "pending" and not port.count("change_plan")
    assert frappe.get_doc("Provision Job", j).attempts == 0
    assert runner.run_due_scheduled() == []
    frappe.get_doc("Provision Job", j).args = '{"plan": "pro", "effective_at": "%s"}' % past
    d = frappe.get_doc("Provision Job", j); d.args = '{"plan": "pro", "effective_at": "%s"}' % past; d.save()
    assert runner.run_due_scheduled() == [j]
    assert runner.run_job(j, port=port) == "succeeded" and port.count("change_plan") == 1


def test_restore_kind_requires_approval_and_runs(frappe):
    runner, _, Fake, _ = mods()
    from saas_core.provisioning.stores import FrappeApprovalStore
    seed(frappe, status="active", fc_site="acme.apps.test"); port = Fake()
    port.sites["acme.apps.test"] = {"status": "active"}
    j0 = job(frappe, kind="restore", args='{"backup": "bk1", "restore": true}')
    assert runner.run_job(j0, port=port) == "needs_attention" and not port.count("restore_backup")
    ap = FrappeApprovalStore(roles_for=lambda u: ["SaaS Admin"])
    a = ap.request("restore_backup", "acme", {"backup": "bk1", "restore": True}, "alice")
    ap.approve(a, "bob"); ap.approve(a, "carol")
    j1 = job(frappe, kind="restore", args='{"backup": "bk1", "restore": true}', approval=a)
    assert runner.run_job(j1, port=port) == "succeeded" and port.count("restore_backup") == 1


def test_no_site_domain_uses_fc_default(frappe):
    runner, _, Fake, _ = mods()
    seed(frappe); frappe.conf.pop("saas_core_site_domain", None)
    j = job(frappe); port = Fake()
    assert runner.build_spec(frappe.get_doc("Tenant", "acme"), "wf").site == "acme"
    assert runner.build_spec(frappe.get_doc("Tenant", "acme"), "wf").domain is None
    assert runner.run_job(j, port=port) == "succeeded"
    t = frappe.get_doc("Tenant", "acme")
    assert t.status == "active" and t.fc_site == "acme.fc.localhost" and "acme.fc.localhost" in port.sites


def test_site_domain_set_still_used(frappe):
    runner, _, Fake, _ = mods()
    seed(frappe)
    spec = runner.build_spec(frappe.get_doc("Tenant", "acme"), "wf")
    assert spec.site == "acme.apps.test" and spec.domain == "apps.test"


def test_effective_at_compared_in_utc_not_site_local(frappe):
    """effective_at is naive UTC; with an IST site clock a change due in 3h must not run yet."""
    import datetime as dt
    runner, _, Fake, _ = mods()
    seed(frappe, status="active", fc_site="acme.apps.test"); port = Fake()
    port.sites["acme.apps.test"] = {"status": "active"}
    soon = (dt.datetime.utcnow() + dt.timedelta(hours=3)).isoformat()
    j = job(frappe, kind="change_plan", args='{"plan": "pro", "effective_at": "%s"}' % soon)
    assert runner.run_due_scheduled() == []
    assert runner.run_job(j, port=port) == "pending" and not port.count("change_plan")


def test_reconcile_stops_retrying_outcome_unknown_after_cap(frappe):
    """A provision job whose resume is refused deterministically must not be re-enqueued forever."""
    _, reconcile, Fake, _ = mods()
    seed(frappe)
    port = Fake()
    j = job(frappe, state="outcome_unknown", workflow_id="wf-x", attempts=reconcile.DEFAULT_MAX_ATTEMPTS,
            error="outcome unknown: resume of wf-x refused: recipe/hook versions changed")
    n_enq = len(frappe._store.enqueued)
    assert reconcile.run(port=port)[j] == "needs_attention"
    d = frappe.get_doc("Provision Job", j)
    assert d.state == "needs_attention" and d.needs_attention == 1
    assert "auto-retry limit" in d.error and "refused" in d.error
    assert len(frappe._store.enqueued) == n_enq
    assert tenant_status(frappe) != "failed"
    assert j not in reconcile.run(port=port)  # needs_attention is not swept again


def test_reconcile_change_plan_loop_also_capped(frappe):
    _, reconcile, Fake, _ = mods()
    seed(frappe, status="active", fc_site="acme.apps.test")
    j = job(frappe, kind="change_plan", state="outcome_unknown", attempts=reconcile.DEFAULT_MAX_ATTEMPTS)
    assert reconcile.run(port=Fake())[j] == "needs_attention"


def test_reconcile_below_cap_still_retries(frappe):
    _, reconcile, Fake, _ = mods()
    seed(frappe)
    j = job(frappe, state="outcome_unknown", workflow_id="wf-x", attempts=reconcile.DEFAULT_MAX_ATTEMPTS - 1)
    assert reconcile.run(port=Fake())[j] == "redispatched"
