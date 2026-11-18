"""Cycle data recorded on payment, renewal receipt routing, day-end cancel, Refund reconciliation."""
import datetime as dt, sys, types, pytest

T0 = dt.datetime(2026, 1, 1)
RZ = "razorpay"
START, END = 1767225600, 1769904000  # 2026-01-01 / 2026-02-01 UTC


@pytest.fixture
def jobs(frappe, monkeypatch):
    calls = []
    m = types.ModuleType("saas_core.jobs")
    m.enqueue_job = lambda tenant, kind, **k: calls.append((tenant, kind)) or "PJ"
    monkeypatch.setitem(sys.modules, "saas_core.jobs", m)
    return calls


def mk(frappe, d, **kw):
    return frappe.get_doc({"doctype": d, **kw}).insert()


def setup(frappe, **sub):
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")
    return mk(frappe, "Subscription", name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway=RZ,
              gateway_sub_id="sub_1", status="active", interval="month", **sub)


def pay_ev(frappe, eid, pid="pay_1", amount=500, start=START, end=END, inv="in_1"):
    rsub = {k: v for k, v in (("current_start", start), ("current_end", end)) if v}
    raw = {"payload": {"payment": {"entity": {"id": pid, "amount": amount, "currency": "INR", "invoice_id": inv}},
                       "subscription": {"entity": rsub}}}
    return mk(frappe, "Payment Event", name=f"{RZ}:{eid}", event_key=f"{RZ}:{eid}", gateway=RZ, event_id=eid,
              kind="payment.succeeded", status="received", subscription_ref="sub_1", invoice_ref=inv,
              amount=amount, currency="INR", raw_payload=raw).name


def run(name):
    from saas_core.billing import processor
    processor.process_payment_event(name, now=T0)


def mails(frappe):
    return [m for m, _ in frappe._store.enqueued]


def test_first_payment_records_cycle_and_sends_invoice_paid(frappe, jobs):
    s = setup(frappe)
    run(pay_ev(frappe, "e1"))
    assert s.period_start == dt.datetime(2026, 1, 1) and s.period_end == dt.datetime(2026, 2, 1)
    assert s.paid_amount == 500 and s.gateway_payment_id == "pay_1"
    inv = frappe.get_all("Invoice")[0]
    assert inv.gateway_payment_id == "pay_1"
    assert "saas_core.notifications.billing_mail.send_invoice_paid" in mails(frappe)
    assert not any("renewal" in m for m in mails(frappe))


def test_callback_values_not_overwritten_by_webhook(frappe, jobs):
    s = setup(frappe, period_start=dt.datetime(2026, 1, 1, 3), paid_amount=499, gateway_payment_id="pay_cb")
    run(pay_ev(frappe, "e1"))
    assert s.period_start == dt.datetime(2026, 1, 1, 3) and s.paid_amount == 499 and s.gateway_payment_id == "pay_cb"
    assert s.period_end == dt.datetime(2026, 2, 1)


def test_webhook_fills_missing_after_callback(frappe, jobs):
    s = setup(frappe, paid_amount=0)
    run(pay_ev(frappe, "e1"))
    assert s.paid_amount == 500 and s.period_start is not None


def test_renewal_replaces_cycle_clears_plan_change_and_sends_receipt(frappe, jobs):
    s = setup(frappe, period_start=dt.datetime(2026, 1, 1), paid_amount=500, gateway_payment_id="pay_1",
              pending_plan="pro", plan_change_state="scheduled", plan_change_at=dt.datetime(2026, 1, 20),
              plan_change_link_id="plink_1", plan_change_amount=100)
    s.period_end = dt.datetime(2026, 2, 1)
    run(pay_ev(frappe, "e1"))  # first
    frappe._store.enqueued.clear()
    s.pending_plan, s.plan_change_state = "pro", "scheduled"
    run(pay_ev(frappe, "e2", pid="pay_2", amount=700, start=END, end=END + 2678400, inv="in_2"))
    assert s.paid_amount == 700 and s.gateway_payment_id == "pay_2"
    assert s.period_start == dt.datetime(2026, 2, 1)
    assert not s.pending_plan and not s.plan_change_state and not s.plan_change_amount and not s.plan_change_link_id
    assert mails(frappe) == ["saas_core.notifications.billing_mail.send_renewal_receipt"]


def test_renewal_without_current_start_uses_previous_period_end(frappe, jobs):
    s = setup(frappe)
    run(pay_ev(frappe, "e1"))
    run(pay_ev(frappe, "e2", pid="pay_2", amount=600, start=None, end=END + 2678400, inv="in_2"))
    assert s.period_start == dt.datetime(2026, 2, 1) and s.paid_amount == 600


def test_duplicate_event_is_idempotent(frappe, jobs):
    s = setup(frappe)
    run(pay_ev(frappe, "e1"))
    run(pay_ev(frappe, "e1b"))  # same invoice/payment replayed under a new event id
    assert s.paid_amount == 500 and s.gateway_payment_id == "pay_1"
    assert mails(frappe).count("saas_core.notifications.billing_mail.send_invoice_paid") == 1
    assert len(frappe.get_all("Invoice")) == 1


def canceled(frappe, eff):
    s = setup(frappe, **({"cancel_effective_at": eff} if eff else {}))
    mk(frappe, "Payment Event", name="rz:c", event_key="rz:c", gateway=RZ, event_id="c", kind="subscription.canceled",
       status="received", subscription_ref="sub_1", tenant_ref="acme")
    run("rz:c")
    return s


def test_cancel_future_effective_keeps_active(frappe, jobs):
    s = canceled(frappe, dt.datetime(2999, 1, 1))
    assert s.status == "active" and s.last_event_at == T0 and jobs == []


@pytest.mark.parametrize("eff", [None, dt.datetime(2000, 1, 1)])
def test_cancel_absent_or_past_cancels(frappe, jobs, eff):
    s = canceled(frappe, eff)
    assert s.status == "canceled" and jobs == [("acme", "suspend")]


def refund_ev(frappe, event="refund.processed", rid="rfnd_1", payid="pay_1", amount=300, status=None, inv=None, tag=""):
    ent = {"id": rid, "payment_id": payid, "amount": amount, "status": status or ("failed" if "failed" in event else "processed")}
    raw = {"event": event, "payload": {"refund": {"entity": ent}}}
    n = f"rz:{event}:{rid}{tag}"
    mk(frappe, "Payment Event", name=n, event_key=n, gateway=RZ, event_id=n, kind="refund", status="received",
       invoice_ref=inv, raw_payload=raw)
    run(n)


def test_refund_processed_by_id(frappe, jobs):
    s = setup(frappe, refunded_amount=50)
    r = mk(frappe, "Refund", subscription="SUB-1", gateway_refund_id="rfnd_1", gateway_payment_id="pay_1", amount=300,
           status="pending")
    refund_ev(frappe)
    assert r.status == "processed" and r.processed_at == T0 and s.refunded_amount == 350
    refund_ev(frappe, tag="b")  # replay: nothing more
    assert s.refunded_amount == 350
    assert mails(frappe) == ["saas_core.notifications.billing_mail.send_refund_processed"]


def test_refund_found_by_payment_and_amount(frappe, jobs):
    s = setup(frappe)
    r = mk(frappe, "Refund", subscription="SUB-1", gateway_payment_id="pay_1", amount=300, status="pending")
    refund_ev(frappe)
    assert r.status == "processed" and r.gateway_refund_id == "rfnd_1" and s.refunded_amount == 300


def test_refund_failed_stores_error(frappe, jobs):
    s = setup(frappe)
    r = mk(frappe, "Refund", subscription="SUB-1", gateway_refund_id="rfnd_1", amount=300, status="pending")
    refund_ev(frappe, event="refund.failed")
    assert r.status == "failed" and r.error and not s.get("refunded_amount") and mails(frappe) == []


def test_refund_unknown_is_quiet(frappe, jobs):
    setup(frappe)
    refund_ev(frappe, rid="rfnd_x", payid="pay_x")
    assert frappe.get_doc("Payment Event", "rz:refund.processed:rfnd_x").status == "processed"


def test_refund_legacy_invoice(frappe, jobs):
    setup(frappe)
    inv = mk(frappe, "Invoice", gateway=RZ, gateway_invoice_id="in_1", status="paid")
    refund_ev(frappe, rid="rfnd_y", payid="pay_y", inv="in_1")
    assert inv.status == "refunded"
