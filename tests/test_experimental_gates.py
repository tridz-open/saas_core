import pytest

pytestmark = pytest.mark.strict_experimental


def test_enabled_parsing(frappe):
    from saas_core import experimental as x
    assert not x.enabled("dunning")
    frappe.conf["saas_core_experimental"] = "dunning, sse"
    assert x.enabled("dunning") and x.enabled("sse") and not x.enabled("restore")
    frappe.conf["saas_core_experimental"] = "all"
    assert x.enabled("restore")
    frappe.conf["saas_core_experimental"] = ""
    frappe.conf["saas_core_experimental_restore"] = 1
    assert x.enabled("restore") and not x.enabled("sse")


def test_sweeps_are_noops_by_default(frappe):
    from saas_core.billing import dunning, expiry, processor
    assert dunning.run_daily() == 0
    assert expiry.sweep_expired() == {"canceled": 0, "dunned": 0, "flagged": 0}
    assert processor.issue_due_invoices() == 0


@pytest.mark.parametrize("mod,fn,args", [
    ("saas_core.api.v1.support.tickets", "list", {}),
    ("saas_core.api.v1.tenants.team", "list", {"tenant": "x"}),
    ("saas_core.api.v1.tenants.backups", "restore", {"tenant": "x", "backup": "b"}),
    ("saas_core.api.v1.tenants.backups", "download_link", {"tenant": "x", "backup": "b", "part": "database"}),
])
def test_experimental_endpoints_403_by_default(frappe, mod, fn, args):
    import importlib
    import tests._api_helpers as h
    h.install(frappe)
    m = importlib.import_module(mod)
    err = importlib.import_module("saas_core.api.v1._common").ApiError
    with pytest.raises(err) as e:
        getattr(m, fn)(**args)
    assert e.value.code == "experimental_disabled" and e.value.http_status == 403


def test_adapter_experimental_paths_disabled(frappe):
    from saas_core.experimental import ExperimentalDisabled
    from saas_core.provisioning.stores import FcctlAdapter
    a = FcctlAdapter(client=object(), server="srv")
    with pytest.raises(ExperimentalDisabled):
        a.create_upload("site")
    assert a.trust_tier() is None
