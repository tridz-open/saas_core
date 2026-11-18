import sys, types, datetime as dt, pytest

T0 = dt.datetime(2026, 1, 1)


@pytest.fixture
def jobs(frappe, monkeypatch):
    calls = []
    m = types.ModuleType("saas_core.jobs")
    def enqueue_job(tenant, kind, *, args=None, approval=None, dedupe_key=None):
        calls.append((tenant, kind, dedupe_key)); return f"PJ-{len(calls)}"
    m.enqueue_job = enqueue_job
    monkeypatch.setitem(sys.modules, "saas_core.jobs", m)
    return calls


def mk(frappe, dt_, **kw):
    return frappe.get_doc({"doctype": dt_, **kw}).insert()


def base(frappe, **kw):
    mk(frappe, "Plan", name="pro", code="pro", available=1)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")
    d = dict(tenant="acme", owner_user="u@x.com", plan="pro", status="active", period_end=T0, billing_mode="invoice")
    d.update(kw)
    return mk(frappe, "Subscription", **d)


def get(frappe, name):
    return frappe.get_doc("Subscription", name)


def test_cancel_at_period_end_expires(frappe, jobs):
    from saas_core.billing import expiry
    s = base(frappe, cancel_at_period_end=1)
    assert expiry.sweep_expired(now=T0 - dt.timedelta(hours=1))["canceled"] == 0
    assert get(frappe, s.name).status == "active"
    assert expiry.sweep_expired(now=T0 + dt.timedelta(hours=1))["canceled"] == 1
    assert get(frappe, s.name).status == "canceled"
    assert jobs == [("acme", "suspend", "suspend:canceled:acme")]
    assert expiry.sweep_expired(now=T0 + dt.timedelta(days=2))["canceled"] == 0


def test_unpaid_invoice_beyond_grace_dunned(frappe, jobs):
    from saas_core.billing import expiry
    s = base(frappe)
    mk(frappe, "Invoice", tenant="acme", subscription=s.name, status="open", issued_at=T0, total_amount=1)
    assert expiry.sweep_expired(now=T0 + dt.timedelta(hours=3))["dunned"] == 0
    assert get(frappe, s.name).status == "active"
    assert expiry.sweep_expired(now=T0 + dt.timedelta(days=2))["dunned"] == 1
    assert get(frappe, s.name).status == "past_due"


def test_gateway_sub_stale_flagged(frappe, jobs):
    from saas_core.billing import expiry
    s = base(frappe, billing_mode="subscription")
    assert expiry.sweep_expired(now=T0 + dt.timedelta(days=2))["flagged"] == 0
    assert expiry.sweep_expired(now=T0 + dt.timedelta(days=4))["flagged"] == 1
    s2 = get(frappe, s.name)
    s2.last_event_at = T0 + dt.timedelta(days=31)
    s2.period_end = T0 + dt.timedelta(days=30)
    s2.save()
    assert expiry.sweep_expired(now=T0 + dt.timedelta(days=40))["flagged"] == 0


def test_dunning_skips_canceled(frappe, jobs):
    from saas_core.billing import dunning
    s = base(frappe, status="canceled")
    assert dunning.on_payment_failed(s.name, T0) is False
    x = get(frappe, s.name)
    assert x.status == "canceled" and not x.get("first_failure_at")
    dunning._advance(x, T0 + dt.timedelta(days=20))
    assert jobs == []


def test_scheduler_registered():
    from saas_core import hooks
    assert "saas_core.billing.expiry.sweep_expired" in hooks.scheduler_events["daily"]
