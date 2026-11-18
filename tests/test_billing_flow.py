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


def setup(frappe, trial=0, status="pending"):
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=trial)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status=status, plan="pro")


def ev(frappe, eid, kind, **kw):
    return mk(frappe, "Payment Event", name=f"stripe:{eid}", event_key=f"stripe:{eid}", gateway="stripe",
              event_id=eid, kind=kind, status="received", tenant_ref="acme", subscription_ref="s1", **kw).name


def run(name, now=None):
    from saas_core.billing import processor
    processor.process_payment_event(name, now=now)


def sub(frappe):
    return frappe.get_all("Subscription")[0]


def test_checkout_then_pay_provisions_and_replay_noop(frappe, jobs):
    setup(frappe)
    run(ev(frappe, "e1", "checkout.completed"), T0)
    assert sub(frappe).status == "active"
    pay = ev(frappe, "e2", "payment.succeeded", amount=100, invoice_ref="in1") if False else None
    e = mk(frappe, "Payment Event", name="stripe:e2", event_key="stripe:e2", gateway="stripe", event_id="e2",
           kind="payment.succeeded", status="received", tenant_ref="acme", subscription_ref="s1",
           invoice_ref="in1", amount=100, currency="usd")
    run(e.name, T0)
    run(e.name, T0)
    assert e.status == "processed" and e.attempts == 1
    assert len(frappe.get_all("Invoice")) == 1
    assert jobs == [("acme", "provision", "provision:acme")]
    # new event, same invoice -> still one invoice
    e3 = mk(frappe, "Payment Event", name="stripe:e3", event_key="stripe:e3", gateway="stripe", event_id="e3",
            kind="payment.succeeded", status="received", tenant_ref="acme", subscription_ref="s1", invoice_ref="in1", amount=100)
    run(e3.name, T0)
    assert len(frappe.get_all("Invoice")) == 1


def test_trial(frappe, jobs):
    setup(frappe, trial=14)
    run(ev(frappe, "e1", "checkout.completed"), T0)
    s = sub(frappe)
    assert s.status == "trialing" and s.trial_end == T0 + dt.timedelta(days=14)


def test_dunning_suspend_resume(frappe, jobs):
    setup(frappe, status="active")
    run(ev(frappe, "e1", "checkout.completed"), T0)
    run(ev(frappe, "f1", "payment.failed"), T0)
    s = sub(frappe)
    assert s.status == "past_due" and s.first_failure_at == T0
    assert s.next_retry_at == T0 + dt.timedelta(days=1) and s.suspend_date == T0 + dt.timedelta(days=7)
    from saas_core.billing import dunning
    dunning.run_daily(T0 + dt.timedelta(days=2))
    assert s.dunning_stage == 1 and s.next_retry_at == T0 + dt.timedelta(days=3)
    dunning.run_daily(T0 + dt.timedelta(days=4))
    dunning.run_daily(T0 + dt.timedelta(days=5))
    assert s.dunning_stage == 2 or s.dunning_stage == 2
    dunning.run_daily(T0 + dt.timedelta(days=8))
    dunning.run_daily(T0 + dt.timedelta(days=9))
    assert s.dunning_stage == 3
    assert [j for j in jobs if j[1] == "suspend"] == [("acme", "suspend", "suspend:dunning:acme")]
    tenant = frappe.get_doc("Tenant", "acme"); tenant.status = "suspended"  # job runner effect
    dunning.run_daily(T0 + dt.timedelta(days=15))
    assert s.dunning_stage == 4 and s.next_retry_at is None and s.suspend_date == T0 + dt.timedelta(days=7)
    e = mk(frappe, "Payment Event", name="stripe:p", event_key="stripe:p", gateway="stripe", event_id="p",
           kind="payment.succeeded", status="received", tenant_ref="acme", subscription_ref="s1", invoice_ref="in9", amount=5)
    run(e.name, T0 + dt.timedelta(days=16))
    assert s.status == "active" and s.dunning_stage == 0 and s.first_failure_at is None
    assert s.next_retry_at is None and s.suspend_date is None
    assert [j[1] for j in jobs if j[1] == "resume"] == ["resume"]


def test_cancel(frappe, jobs):
    setup(frappe, status="active")
    run(ev(frappe, "e1", "checkout.completed"), T0)
    run(ev(frappe, "c", "subscription.canceled"), T0)
    assert sub(frappe).status == "canceled"
    assert ("acme", "suspend", "suspend:canceled:acme") in jobs
    assert frappe.get_doc("Tenant", "acme").suspend_reason == "canceled"


def test_failure_marks_failed_and_reraises(frappe, jobs):
    setup(frappe)
    e = mk(frappe, "Payment Event", name="stripe:x", event_key="stripe:x", gateway="stripe", event_id="x",
           kind="checkout.completed", status="received", tenant_ref="ghost", subscription_ref="nope")
    with pytest.raises(ValueError):
        run(e.name)
    assert e.status == "failed" and e.attempts == 1 and "unknown tenant" in e.error


def test_unknown_subscription_is_retryable_not_failed(frappe, jobs):
    setup(frappe)
    e = mk(frappe, "Payment Event", name="stripe:y", event_key="stripe:y", gateway="stripe", event_id="y",
           kind="payment.failed", status="received", tenant_ref="acme", subscription_ref="nope")
    run(e.name)  # transient: no raise, stays received with a backoff deadline
    assert e.status == "received" and e.attempts == 1 and "not found" in e.error and e.next_retry_at


def test_refund_and_ignored(frappe, jobs):
    setup(frappe, status="active")
    run(ev(frappe, "e1", "checkout.completed"), T0)
    p = mk(frappe, "Payment Event", name="stripe:p", event_key="stripe:p", gateway="stripe", event_id="p",
           kind="payment.succeeded", status="received", tenant_ref="acme", subscription_ref="s1", invoice_ref="i", amount=5)
    run(p.name, T0)
    r = mk(frappe, "Payment Event", name="stripe:r", event_key="stripe:r", gateway="stripe", event_id="r",
           kind="refund", status="received", invoice_ref="i")
    run(r.name)
    assert frappe.get_all("Invoice")[0].status == "refunded"
    g = mk(frappe, "Payment Event", name="stripe:g", event_key="stripe:g", gateway="stripe", event_id="g", kind="ignored", status="received")
    run(g.name)
    assert g.status == "ignored"
