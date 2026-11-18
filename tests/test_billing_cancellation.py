"""Day-based cancellation engine: quote, cancel ordering/failure modes, sweep_due, legacy fallbacks."""
import datetime as dt
import sys
import types

import pytest

START = dt.datetime(2026, 2, 28, 18, 30)   # 2026-03-01 00:00 IST
END = dt.datetime(2026, 3, 31, 18, 30)     # 2026-04-01 00:00 IST (31 day cycle)
MID = dt.datetime(2026, 3, 10, 12, 0)      # IST day 10 of the cycle
SAME_DAY = dt.datetime(2026, 2, 28, 19, 0)  # IST Mar 1, first day
LAST_DAY = dt.datetime(2026, 3, 31, 6, 0)   # IST Mar 31, last day


class FakeGW:
    def __init__(self):
        self.calls, self.fail_cancel, self.fail_refund, self.refund_status = [], False, False, "pending"
        self.lookup = "pay_looked_up"

    def cancel_subscription(self, ref, at_end):
        self.calls.append(("cancel", ref, at_end))
        if self.fail_cancel:
            raise RuntimeError("boom")

    def refund_payment(self, pid, amount, *, notes=None, receipt=""):
        self.calls.append(("refund", pid, amount, notes, receipt))
        if self.fail_refund:
            raise RuntimeError("refund boom")
        return {"id": "rfnd_1", "status": self.refund_status, "amount": amount, "payment_id": pid}

    def invoice_payment_id(self, inv):
        self.calls.append(("lookup", inv))
        if self.lookup is None:
            raise RuntimeError("no lookup")
        return self.lookup


@pytest.fixture
def env(frappe, monkeypatch):
    gw = FakeGW()
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name=None, account=None: gw
    jobs, audit, events = [], [], []
    jm = types.ModuleType("saas_core.jobs")
    jm.enqueue_job = lambda t, k, *, args=None, approval=None, dedupe_key=None: jobs.append((t, k, args, dedupe_key))
    am = types.ModuleType("saas_core.audit")
    am.log = lambda action, **kw: audit.append((action, kw))
    em = types.ModuleType("saas_core.events")
    em.publish = lambda type, **kw: events.append((type, kw))
    for n, m in (("saas_core.gateways.registry", reg), ("saas_core.jobs", jm), ("saas_core.audit", am),
                 ("saas_core.events", em)):
        monkeypatch.setitem(sys.modules, n, m)
    mk(frappe, "Plan", name="pro", code="pro", title="Pro")
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")
    return types.SimpleNamespace(f=frappe, gw=gw, jobs=jobs, audit=audit, events=events)


def mk(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


def sub(env, **kw):
    d = dict(name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway="razorpay", gateway_sub_id="sub_1",
             status="active", interval="month", currency="INR", period_start=START, period_end=END,
             paid_amount=3100, gateway_payment_id="pay_1")
    d.update(kw)
    return mk(env.f, "Subscription", **d)


def refunds(env):
    return [d for (t, _), d in env.f._store.docs.items() if t == "Refund"]


def test_quote_mid_cycle_does_not_mutate(env):
    from saas_core.billing import cancellation
    s = sub(env)
    snap = dict(s)
    q = cancellation.quote(s, MID)
    assert (q["days_used"], q["days_unused"], q["refund_minor"], q["charge_minor"]) == (10, 21, 2100, 1000)
    assert q["can_cancel"] and q["blocked_reason"] is None and q["refund_to"] == "original payment method"
    assert q["stops_at"] == "2026-03-10T18:30:00Z" and q["renewal_date"] == "2026-04-01"
    assert q["plan_label"] == "Pro" and q["currency"] == "INR" and q["timezone"] == "Asia/Kolkata"
    assert dict(s) == snap and not refunds(env) and env.gw.calls == []


def test_same_day_and_last_day_quotes(env):
    from saas_core.billing import cancellation
    s = sub(env)
    assert cancellation.quote(s, SAME_DAY)["refund_minor"] == 3000
    last = cancellation.quote(s, LAST_DAY)
    assert last["refund_minor"] == 0 and last["days_unused"] == 0


def test_cancel_mid_cycle_refunds_and_schedules_stop(env):
    from saas_core.billing import cancellation
    s = sub(env)
    out = cancellation.cancel(s, "u@x.com", reason="too dear", now=MID)
    assert env.gw.calls[0] == ("cancel", "sub_1", False)
    assert env.gw.calls[1][:3] == ("refund", "pay_1", 2100)
    r = refunds(env)[0]
    assert env.gw.calls[1][4] == r["name"] and env.gw.calls[1][3]["reason"] == "cancel"
    assert r["status"] == "pending" and r["gateway_refund_id"] == "rfnd_1" and r["kind"] == "cancel"
    assert (r["days_used"], r["days_unused"], r["amount"]) == (10, 21, 2100)
    assert out["status"] == "canceling" and out["refund"] == {"id": r["name"], "status": "pending",
                                                               "amount_minor": 2100, "currency": "INR"}
    assert s["status"] == "active" and s["cancel_effective_at"] == dt.datetime(2026, 3, 10, 18, 30)
    assert s["cancel_at_period_end"] == 1 and s["cancel_reason"] == "too dear" and s["canceled_at"] == MID
    assert env.audit[0][0] == "subscription.cancel" and env.audit[0][1]["details"]["refund_minor"] == 2100
    assert env.events[0][0] == "subscription.updated"
    assert env.f._store.enqueued[-1] == ("saas_core.notifications.billing_mail.send_cancellation",
                                         {"subscription": "SUB-1", "refund": r["name"], "enqueue_after_commit": True})
    assert env.f._store.commits >= 1


def test_processed_refund_adds_refunded_amount(env):
    from saas_core.billing import cancellation
    env.gw.refund_status = "processed"
    s = sub(env)
    out = cancellation.cancel(s, "u@x.com", now=MID)
    assert out["refund"]["status"] == "processed" and s["refunded_amount"] == 2100
    assert refunds(env)[0]["processed_at"]


def test_last_day_cancel_has_no_refund(env):
    from saas_core.billing import cancellation
    s = sub(env)
    out = cancellation.cancel(s, "u@x.com", now=LAST_DAY)
    assert out["refund"] is None and not refunds(env)
    assert [c[0] for c in env.gw.calls] == ["cancel"]
    assert s["cancel_effective_at"] == dt.datetime(2026, 3, 31, 18, 30)


def test_trial_no_refund(env):
    from saas_core.billing import cancellation
    s = sub(env, status="trialing", paid_amount=3100)
    q = cancellation.quote(s, MID)
    assert q["refund_minor"] == 0 and q["paid_minor"] == 0
    assert cancellation.cancel(s, "u@x.com", now=MID)["refund"] is None


def test_already_canceling_blocks(env):
    from saas_core.billing import cancellation
    s = sub(env, cancel_effective_at=dt.datetime(2026, 3, 10, 18, 30))
    assert cancellation.quote(s, MID)["blocked_reason"] == "already_canceling"
    with pytest.raises(cancellation.CancelError) as e:
        cancellation.cancel(s, "u@x.com", now=MID)
    assert e.value.code == "already_canceling" and env.gw.calls == []
    s2 = sub(env, name="SUB-2", status="canceled")
    assert cancellation.quote(s2, MID)["can_cancel"] is False


def test_scheduled_and_awaiting_plan_change_block(env):
    from saas_core.billing import cancellation
    s = sub(env, plan_change_state="scheduled")
    with pytest.raises(cancellation.CancelError) as e:
        cancellation.cancel(s, "u@x.com", now=MID)
    assert e.value.code == "plan_change_pending"
    s2 = sub(env, name="SUB-2", plan_change_state="awaiting_payment", pending_plan="pro")
    q = cancellation.quote(s2, MID)
    assert q["can_cancel"] is False and q["blocked_reason"] == "plan_change_pending_payment"
    with pytest.raises(cancellation.CancelError) as e2:
        cancellation.cancel(s2, "u@x.com", now=MID)
    assert e2.value.code == "plan_change_pending_payment" and s2["plan_change_state"] == "awaiting_payment"
    assert s2["pending_plan"] == "pro" and env.gw.calls == []


def test_gateway_cancel_failure_changes_nothing(env):
    from saas_core.billing import cancellation
    env.gw.fail_cancel = True
    s = sub(env)
    with pytest.raises(cancellation.CancelError) as e:
        cancellation.cancel(s, "u@x.com", now=MID)
    assert e.value.code == "gateway_error"
    assert not s.get("cancel_effective_at") and not s.get("canceled_at") and not refunds(env)
    assert not env.audit and not env.events and not env.f._store.enqueued
    assert [c[0] for c in env.gw.calls] == ["cancel"]


def test_refund_failure_keeps_cancel_and_flags(env):
    from saas_core.billing import cancellation
    env.gw.fail_refund = True
    s = sub(env)
    out = cancellation.cancel(s, "u@x.com", now=MID)
    assert s["cancel_effective_at"] and out["status"] == "canceling"
    assert out["refund"]["status"] == "failed"
    assert refunds(env)[0]["error"].startswith("attempt 1:")
    assert [e[0] for e in env.events] == ["billing.needs_attention", "subscription.updated"]
    assert env.f._store.enqueued


def test_legacy_fallback_payment_lookup_via_invoice(env):
    from saas_core.billing import cancellation
    s = sub(env, period_start=None, paid_amount=None, gateway_payment_id=None)
    mk(env.f, "Invoice", name="INV-1", subscription="SUB-1", tenant="acme", status="paid", total_amount=3100,
       currency="INR", gateway_invoice_id="inv_1", creation=1)
    q = cancellation.quote(s, MID)
    assert q["cycle_start"] == "2026-03-01" and q["paid_minor"] == 3100 and q["refund_minor"] == 2100
    cancellation.cancel(s, "u@x.com", now=MID)
    assert ("lookup", "inv_1") in env.gw.calls and ("refund", "pay_looked_up") == env.gw.calls[-1][:2]


def test_legacy_invoice_payment_id_preferred(env):
    from saas_core.billing import cancellation
    s = sub(env, paid_amount=None, gateway_payment_id=None)
    mk(env.f, "Invoice", name="INV-1", subscription="SUB-1", tenant="acme", status="paid", total_amount=3100,
       gateway_payment_id="pay_inv", gateway_invoice_id="inv_1", creation=1)
    cancellation.cancel(s, "u@x.com", now=MID)
    assert env.gw.calls[-1][1] == "pay_inv" and not any(c[0] == "lookup" for c in env.gw.calls)


def test_payment_unknown_cancels_without_refund_and_flags(env):
    from saas_core.billing import cancellation
    env.gw.lookup = None
    s = sub(env, paid_amount=None, gateway_payment_id=None)
    mk(env.f, "Invoice", name="INV-1", subscription="SUB-1", tenant="acme", status="paid", total_amount=3100,
       gateway_invoice_id="inv_1", creation=1)
    out = cancellation.cancel(s, "u@x.com", now=MID)
    assert out["refund"] == {"id": refunds(env)[0]["name"], "status": "failed", "amount_minor": 2100,
                             "currency": "INR"}
    assert s["cancel_effective_at"] and len(refunds(env)) == 1
    r = refunds(env)[0]
    assert r["status"] == "failed" and r["error"] == "payment id unknown" and r["kind"] == "cancel"
    assert not any(c[0] == "refund" for c in env.gw.calls)
    assert env.events[0][0] == "billing.needs_attention"


def test_legacy_year_interval_start(env):
    from saas_core.billing import cancellation
    s = sub(env, interval="year", period_start=None, period_end=dt.datetime(2027, 2, 28, 18, 30), paid_amount=36500)
    q = cancellation.quote(s, MID)
    assert q["cycle_start"] == "2026-03-01" and q["cycle_days"] == 365


def test_no_period_end(env):
    from saas_core.billing import cancellation
    s = sub(env, period_start=None, period_end=None)
    q = cancellation.quote(s, MID)
    assert q["can_cancel"] and q["refund_minor"] == 0 and q["stops_at"] == "2026-03-10T18:30:00Z"
    out = cancellation.cancel(s, "u@x.com", now=MID)
    assert out["refund"] is None and s["cancel_effective_at"] == dt.datetime(2026, 3, 10, 18, 30)


def test_sweep_due_finalises_only_after_effective_at(env):
    from saas_core.billing import cancellation
    s = sub(env, cancel_effective_at=dt.datetime(2026, 3, 10, 18, 30))
    assert cancellation.sweep_due(now=MID)["canceled"] == 0 and s["status"] == "active"
    out = cancellation.sweep_due(now=dt.datetime(2026, 3, 10, 18, 31))
    assert out["canceled"] == 1 and s["status"] == "canceled"
    assert env.jobs == [("acme", "suspend", {"reason": "canceled"}, "suspend:canceled:acme")]
    assert env.f.get_doc("Tenant", "acme")["suspend_reason"] == "canceled"
    assert cancellation.sweep_due(now=dt.datetime(2026, 3, 12))["canceled"] == 0


def test_sweep_retries_failed_refund_until_cap(env):
    from saas_core.billing import cancellation
    env.gw.fail_refund = True
    s = sub(env)
    cancellation.cancel(s, "u@x.com", now=MID)
    r = refunds(env)[0]
    env.gw.fail_refund = False
    out = cancellation.sweep_due(now=MID)
    assert out["refunds_retried"] == 1 and r["status"] == "pending" and r["gateway_refund_id"] == "rfnd_1"
    assert env.gw.calls[-1][4] == r["name"]
    # cap: after 5 failed attempts it is left alone
    r["status"], r["error"] = "failed", "attempt 5: boom"
    n = len(env.gw.calls)
    assert cancellation.sweep_due(now=MID)["refunds_retried"] == 0 and len(env.gw.calls) == n


def test_sweep_retry_failure_counts_attempts(env):
    from saas_core.billing import cancellation
    env.gw.fail_refund = True
    s = sub(env)
    cancellation.cancel(s, "u@x.com", now=MID)
    cancellation.sweep_due(now=MID)
    assert refunds(env)[0]["error"].startswith("attempt 2:")


# ---- review fixes -------------------------------------------------------------------------------------------

def test_cancel_persists_before_gateway_and_rolls_back_on_failure(env):
    from saas_core.billing import cancellation
    seen = {}
    orig = env.gw.cancel_subscription

    def spy(ref, at_end):
        seen["eff"], seen["commits"] = s.get("cancel_effective_at"), env.f._store.commits
        orig(ref, at_end)

    env.gw.cancel_subscription = spy
    env.gw.fail_cancel = True
    s = sub(env)
    with pytest.raises(cancellation.CancelError):
        cancellation.cancel(s, "u@x.com", reason="x", now=MID)
    assert seen["eff"] == dt.datetime(2026, 3, 10, 18, 30) and seen["commits"] >= 1
    assert not s.get("cancel_effective_at") and not s.get("canceled_at") and not s.get("cancel_reason")
    assert not s.get("cancel_at_period_end") and not refunds(env)


def test_gateway_already_cancelled_is_success(env):
    from saas_core.billing import cancellation

    def boom(ref, at_end):
        raise RuntimeError("Razorpay 400: This subscription is already cancelled")

    env.gw.cancel_subscription = boom
    s = sub(env)
    out = cancellation.cancel(s, "u@x.com", now=MID)
    assert out["status"] == "canceling" and s["cancel_effective_at"] and len(refunds(env)) == 1


def test_existing_cancel_refund_refuses_second_cancel(env):
    from saas_core.billing import cancellation
    s = sub(env)
    cancellation.cancel(s, "u@x.com", now=MID)
    s["cancel_effective_at"] = None  # e.g. fields lost; the Refund row still proves a cancel happened
    n = len(env.gw.calls)
    with pytest.raises(cancellation.CancelError) as e:
        cancellation.cancel(s, "u@x.com", now=MID)
    assert e.value.code == "already_canceling" and len(env.gw.calls) == n and len(refunds(env)) == 1


def test_cancel_locks_subscription_row(env):
    from saas_core.billing import cancellation
    calls = []
    env.f.db.sql = lambda *a, **k: calls.append(a)
    cancellation.cancel(sub(env), "u@x.com", now=MID)
    assert "for update" in calls[0][0].lower() and calls[0][1] == ("SUB-1",)


def test_cancel_splits_refund_across_extra_payments_newest_first(env):
    import json
    from saas_core.billing import cancellation
    # original 2000 + upgrade link payment 1100 = held 3100; refund 2100 -> 1100 from the link, 1000 from original
    s = sub(env, extra_payments=json.dumps([{"payment_id": "pay_link", "amount": 1100}]))
    out = cancellation.cancel(s, "u@x.com", now=MID)
    calls = [c for c in env.gw.calls if c[0] == "refund"]
    assert [(c[1], c[2]) for c in calls] == [("pay_link", 1100), ("pay_1", 1000)]
    rs = refunds(env)
    assert len(rs) == 2 and {c[4] for c in calls} == {r["name"] for r in rs}
    assert out["refund"]["amount_minor"] == 2100 and out["refund"]["id"] == rs[0]["name"]
    assert not s.get("extra_payments")


def test_cancel_refund_capped_by_each_payment(env):
    import json
    from saas_core.billing import cancellation
    s = sub(env, paid_amount=3100, extra_payments=json.dumps([{"payment_id": "pay_a", "amount": 500},
                                                              {"payment_id": "pay_b", "amount": 400}]))
    cancellation.cancel(s, "u@x.com", now=SAME_DAY)  # refund 3000
    calls = [(c[1], c[2]) for c in env.gw.calls if c[0] == "refund"]
    assert calls == [("pay_b", 400), ("pay_a", 500), ("pay_1", 2100)]


def test_processed_refund_counted_once_pending_not(env):
    from saas_core.billing import cancellation, processor
    s = sub(env)
    cancellation.cancel(s, "u@x.com", now=MID)
    assert not s.get("refunded_amount")  # pending
    r = refunds(env)[0]
    processor._apply_refund(r, {"status": "processed"}, "refund.processed", "rfnd_1", MID)
    processor._apply_refund(r, {"status": "processed"}, "refund.processed", "rfnd_1", MID)
    assert s["refunded_amount"] == 2100


def test_quote_after_cancel_uses_stored_values(env):
    from saas_core.billing import cancellation
    s = sub(env)
    cancellation.cancel(s, "u@x.com", now=MID)
    later = dt.datetime(2026, 3, 10, 17, 0)
    q = cancellation.quote(s, later)
    assert q["blocked_reason"] == "already_canceling"
    assert q["stops_at"] == "2026-03-10T18:30:00Z" and q["refund_minor"] == 2100 and q["charge_minor"] == 1000
    assert (q["days_used"], q["days_unused"]) == (10, 21)
    q2 = cancellation.quote(s, dt.datetime(2026, 3, 20, 12, 0))
    assert q2["stops_at"] == "2026-03-10T18:30:00Z" and q2["refund_minor"] == 2100
    assert (q2["days_used"], q2["days_unused"]) == (10, 21)


def test_sweep_retries_failed_refunds_of_every_kind(env):
    from saas_core.billing import cancellation
    s = sub(env)
    mk(env.f, "Refund", name="R-PC", subscription="SUB-1", tenant="acme", gateway="razorpay",
       gateway_payment_id="pay_1", amount=500, currency="INR", kind="plan_change", status="failed",
       error="attempt 1: boom")
    out = cancellation.sweep_due(now=MID)
    assert out["refunds_retried"] == 1 and env.gw.calls[-1][:3] == ("refund", "pay_1", 500)
    assert env.f.get_doc("Refund", "R-PC")["status"] == "pending" and s["status"] == "active"


def test_cancel_sweep_is_every_minute():
    import pathlib, re
    src = (pathlib.Path(__file__).parent.parent / "saas_core" / "hooks.py").read_text()
    ns = {}
    exec(src, ns)
    cron = ns["scheduler_events"]["cron"]
    assert "saas_core.billing.cancellation.sweep_due" in cron["* * * * *"]
    assert "saas_core.billing.plan_change.sweep_due" in cron["*/15 * * * *"]
    assert "saas_core.billing.cancellation.sweep_due" not in cron["*/15 * * * *"]
