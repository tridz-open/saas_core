import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.j = h.mod("saas_core.api.v1.jobs")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    h.seed(frappe, "mine"); h.seed(frappe, "theirs", owner="bob@x.com")
    yield ns
    h.restore()


def mk(env, name, tenant, **kw):
    return env.f.get_doc({"doctype": "Provision Job", "name": name, "tenant": tenant, "kind": "provision", "state": "running", **kw}).insert()


def test_get_and_list(env):
    mk(env, "PJ-1", "mine", steps=[{"seq": 1, "name": "create", "state": "running"}]); mk(env, "PJ-2", "mine")
    out = env.j.get(job="PJ-1"); h.check_schema("Job", out)
    assert out["steps"][0]["name"] == "create"
    page = env.j.list(tenant="mine", limit=1)
    assert len(page["items"]) == 1 and page["next_cursor"]
    h.check_schema("Job", page["items"][0])
    assert len(env.j.list(tenant="mine")["items"]) == 2


def test_enumeration_safety(env):
    mk(env, "PJ-9", "theirs")
    for call in (lambda: env.j.get(job="PJ-9"), lambda: env.j.get(job="PJ-NOPE"), lambda: env.j.list(tenant="theirs"), lambda: env.j.list(tenant="ghost")):
        with pytest.raises(env.ApiError) as e: call()
        assert e.value.http_status == 404
    with pytest.raises(env.ApiError) as e: env.j.get()
    assert e.value.http_status == 400


@pytest.mark.parametrize("raw", [
    "ConnectionError: https://frappecloud.com/api/method/press.x token=abc",
    "Traceback (most recent call last):\n  File \"/opt/fcctl/x.py\"",
    "fc_server_id n1.example.frappe.cloud refused",
])
def test_error_scrubbed(env, raw):
    mk(env, "PJ-3", "mine", state="failed", error=raw, steps=[{"seq": 1, "name": "s", "state": "failed", "message": raw}])
    out = env.j.get(job="PJ-3"); h.check_schema("Job", out)
    assert out["error"] == "The operation failed. Our team has been notified."
    for s in (out["error"], out["steps"][0]["message"]):
        if s is None: continue
        assert "frappecloud" not in s and "token" not in s and "fcctl" not in s and "frappe.cloud" not in s and "File" not in s


def test_safe_error_kept(env):
    mk(env, "PJ-4", "mine", state="failed", error="Site name is not available")
    assert env.j.get(job="PJ-4")["error"] == "The operation failed. Our team has been notified."
