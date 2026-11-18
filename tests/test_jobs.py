import json
import pytest
from prov_helpers import seed


def test_enqueue_creates_and_dedupes(frappe):
    import importlib; jobs = importlib.import_module("saas_core.jobs")
    seed(frappe)
    j = jobs.enqueue_job("acme", "provision", args={"a": 1}, dedupe_key="k1")
    doc = frappe.get_doc("Provision Job", j)
    assert doc.state == "pending" and json.loads(doc.args) == {"a": 1}
    assert frappe._store.enqueued[0][0] == "saas_core.provisioning.runner.run_job"
    assert frappe._store.enqueued[0][1]["job"] == j
    assert frappe.get_doc("Tenant", "acme").active_job == j
    assert jobs.enqueue_job("acme", "provision", dedupe_key="k1") == j and len(frappe._store.enqueued) == 1
    doc.state = "succeeded"
    j2 = jobs.enqueue_job("acme", "provision", dedupe_key="k1")
    assert j2 != j and len(frappe._store.enqueued) == 2
    assert [d for (dt, _), d in frappe._store.docs.items() if dt == "Audit Event"]


def test_bad_kind(frappe):
    import importlib; jobs = importlib.import_module("saas_core.jobs")
    seed(frappe)
    with pytest.raises(frappe.ValidationError):
        jobs.enqueue_job("acme", "explode")
