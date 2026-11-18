import sys, types, datetime as dt, pytest

T0 = dt.datetime(2026, 1, 1)


@pytest.fixture
def jobs(frappe, monkeypatch):
    calls = []
    m = types.ModuleType("saas_core.jobs")
    def enqueue_job(tenant, kind, *, args=None, approval=None, dedupe_key=None):
        calls.append(tenant)
        frappe.get_doc({"doctype": "Provision Job", "tenant": tenant, "kind": kind, "state": "pending"}).insert()
        frappe.db.set_value("Tenant", tenant, "status", "pending")
        return f"PJ-{len(calls)}"
    m.enqueue_job = enqueue_job
    monkeypatch.setitem(sys.modules, "saas_core.jobs", m)
    return calls


def mk(frappe, dt_, **kw):
    return frappe.get_doc({"doctype": dt_, **kw}).insert()


def setup(frappe, status="pending"):
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status=status, plan="pro")


def ev(frappe, eid, kind, **kw):
    return mk(frappe, "Payment Event", name=f"rzp:{eid}", event_key=f"rzp:{eid}", gateway="rzp", event_id=eid,
              kind=kind, status="received", tenant_ref="acme", subscription_ref="s1", **kw).name


def run(name):
    from saas_core.billing import processor
    processor.process_payment_event(name, now=T0)


def test_callback_and_webhook_provision_once(frappe, jobs):
    setup(frappe)
    kw = dict(amount=100, currency="inr", invoice_ref="in1")
    run(ev(frappe, "cb1", "checkout.completed"))
    run(ev(frappe, "wh1", "checkout.completed"))  # same payment via the other path
    run(ev(frappe, "cb2", "payment.succeeded", **kw))
    run(ev(frappe, "wh2", "payment.succeeded", **kw))
    assert len(frappe.get_all("Subscription")) == 1
    assert frappe.get_all("Subscription")[0].status == "active"
    assert len(jobs) == 1
    assert len(frappe.get_all("Provision Job")) == 1


@pytest.mark.parametrize("status", ["provisioning", "active"])
def test_skip_when_tenant_past_pending(frappe, jobs, status):
    setup(frappe, status=status)
    from saas_core.billing import processor
    processor._maybe_provision("acme")
    assert jobs == []


def test_skip_when_open_job_exists(frappe, jobs):
    setup(frappe)
    mk(frappe, "Provision Job", tenant="acme", kind="provision", state="running")
    from saas_core.billing import processor
    processor._maybe_provision("acme")
    assert jobs == []
