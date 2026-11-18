import json
import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.b = h.mod("saas_core.api.v1.tenants.backups")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    yield ns
    h.restore()


def test_restore_pending_approval(env):
    h.seed(env.f, "acme")
    out = env.b.restore(tenant="acme", backup="bk2")
    h.check_schema("RestoreApproval", out)
    assert env.f.local.response["http_status_code"] == 202
    apr = env.f.get_doc("Approval Request", out["approval_request"])
    assert apr.op == "restore_backup" and apr.target == "acme" and apr.status == "pending"
    assert apr.digest and json.loads(apr.blast_radius)["tenant"] == "acme"
    assert env.enqueued == [] and any(a[0] == "tenant.backup_restore_requested" for a in env.audit)


def test_restore_unknown_backup_and_non_owner(env):
    h.seed(env.f, "acme"); h.seed(env.f, "theirs", owner="bob@x.com")
    with pytest.raises(env.ApiError) as e: env.b.restore(tenant="acme", backup="nope")
    assert e.value.http_status == 404
    with pytest.raises(env.ApiError) as e: env.b.restore(tenant="theirs", backup="bk1")
    assert e.value.http_status == 404
    with pytest.raises(env.ApiError) as e: env.b.restore(tenant="acme")
    assert e.value.http_status == 400


def test_enqueue_restore_after_approval(env):
    h.seed(env.f, "acme")
    out = env.b.restore(tenant="acme", backup="bk1")
    apr = env.f.get_doc("Approval Request", out["approval_request"])
    import importlib.util, pathlib
    sp = importlib.util.spec_from_file_location("_restore_mod", pathlib.Path(__file__).parent.parent / "saas_core" / "provisioning" / "restore.py")
    r = importlib.util.module_from_spec(sp); sp.loader.exec_module(r)
    with pytest.raises(ValueError): r.enqueue_restore_after_approval(apr)
    apr.status = "approved"
    job = env.f.get_doc("Provision Job", r.enqueue_restore_after_approval(apr))
    assert job.kind == "restore" and job.args["restore"] is True and job.args["backup"] == "bk1"
