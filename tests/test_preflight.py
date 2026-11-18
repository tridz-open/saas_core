import json
import pytest
from prov_helpers import seed, job, tenant_status


def test_over_cap_is_capacity_exhausted_and_no_create(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe); frappe.get_doc("Plan", "pro").limits = json.dumps({"fc_monthly_cost": 4000})
    j = job(frappe); port = FakeFcctl()
    port.tier = {"spending_limit": 8300, "running_monthly": 8000, "resources_used": 2, "resource_limit": 3}
    assert runner.run_job(j, port=port) == "needs_attention"
    assert "capacity_exhausted" in frappe.get_doc("Provision Job", j).error
    assert port.count("provision_site") == 0 and port.sites_created == 0
    assert not (frappe.get_doc("Provision Job", j).steps or [])
    assert tenant_status(frappe) == "pending"


def test_resource_cap(frappe):
    from saas_core.provisioning.preflight import check_capacity
    assert "resources" in check_capacity({"resources_used": 3, "resource_limit": 3})
    assert check_capacity({"spending_limit": 8300, "running_monthly": 4000}, 4000) is None
    assert check_capacity(None, 5) is None


def test_headroom_provisions(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe); j = job(frappe); port = FakeFcctl()
    port.tier = {"spending_limit": 8300, "running_monthly": 2000, "resources_used": 1, "resource_limit": 3}
    assert runner.run_job(j, port=port) == "succeeded"


def test_downgrade_disk_guard(frappe):
    from saas_core.provisioning.preflight import DowngradeRefused, assert_plan_change_allowed
    with pytest.raises(DowngradeRefused, match="smaller"):
        assert_plan_change_allowed({"disk_gb": 50}, {"disk_gb": 25})
    assert_plan_change_allowed({"disk_gb": 25}, {"disk_gb": 50})
    assert_plan_change_allowed({}, {"disk_gb": 10})


def test_runner_change_plan_refuses_smaller_disk(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    seed(frappe, status="active", fc_site="acme.apps.test")
    frappe.get_doc("Plan", "pro").limits = json.dumps({"disk_gb": 50})
    frappe.get_doc({"doctype": "Plan", "name": "mini", "code": "mini", "limits": json.dumps({"disk_gb": 25})}).insert()
    port = FakeFcctl(); port.sites["acme.apps.test"] = {"status": "active"}
    j = job(frappe, kind="change_plan", args=json.dumps({"plan": "mini"}))
    assert runner.run_job(j, port=port) == "needs_attention"
    assert port.count("change_plan") == 0
