"""Day-based plan change: preview/apply/link settlement/sweep. Fakes only, no network."""
import datetime as dt, sys, types
import pytest

# Jan 1..Feb 1 2026 = 31 days. Pro 31000, Starter 15500 (1000 / 500 per day).
START, END = dt.datetime(2026, 1, 1), dt.datetime(2026, 2, 1)
NOON_JAN11 = dt.datetime(2026, 1, 11, 6, 30)  # 12:00 IST on Jan 11 -> change effective Jan 12, 20 days remain
RZ = "razorpay"


class FakeGw:
    def __init__(self):
        self.calls, self.fail_update, self.fail_refund = [], False, False

    def create_payment_link(self, **kw):
        self.calls.append(("link", kw))
        return {"id": "plink_1", "short_url": "https://rzp.io/i/x"}

    def refund_payment(self, pid, amount, *, notes=None, receipt=""):
        self.calls.append(("refund", pid, amount, receipt))
        if self.fail_refund:
            raise RuntimeError("boom")
        return {"id": "rfnd_1", "status": "processed", "amount": amount, "payment_id": pid}

    def update_subscription_plan(self, ref, plan_id, *, at_cycle_end=True):
        self.calls.append(("update", ref, plan_id, at_cycle_end))
        if self.fail_update:
            raise RuntimeError("boom")
        return {}


@pytest.fixture
def env(frappe, monkeypatch):
    ns = types.SimpleNamespace(gw=FakeGw(), jobs=[], audit=[], frappe=frappe)
    from saas_core.gateways import registry
    monkeypatch.setattr(registry, "get_gateway", lambda name, *a, **k: ns.gw)
    jm = types.ModuleType("saas_core.jobs")
    jm.enqueue_job = lambda tenant, kind, **k: ns.jobs.append((tenant, kind, k)) or "PJ"
    monkeypatch.setitem(sys.modules, "saas_core.jobs", jm)
    import saas_core
    monkeypatch.setattr(saas_core, "jobs", jm, raising=False)
    am = types.ModuleType("saas_core.audit")
    am.log = lambda action, **k: ns.audit.append((action, k))
    monkeypatch.setitem(sys.modules, "saas_core.audit", am)
    monkeypatch.setattr(saas_core, "audit", am, raising=False)
    return ns


def mk(frappe, d, **kw):
    return frappe.get_doc({"doctype": d, **kw}).insert()


def plan(frappe, name, amount, order, price_id=None):
    mk(frappe, "Plan", name=name, code=name, available=1, sort_order=order, is_public=1, template="erp",
       prices=[{"interval": "month", "currency": "INR", "amount": amount, "gateway": RZ,
                "gateway_price_id": price_id or f"plan_{name}"}])


def setup(frappe, cur="pro", status="active", paid=31000, **kw):
    plan(frappe, "pro", 31000, 2)
    plan(frappe, "starter", 15500, 1)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan=cur)
    base = dict(name="SUB-1", tenant="acme", owner_user="u@x.com", plan=cur, gateway=RZ, gateway_sub_id="sub_1",
                status=status, interval="month", currency="INR", period_start=START, period_end=END,
                paid_amount=paid, gateway_payment_id="pay_1")
    base.update(kw)
    return mk(frappe, "Subscription", **base)


def test_preview_upgrade_payment_required(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc
    p = pc.preview(sub, "pro", now=NOON_JAN11)
    # 20 days remain: old share 10000, new share 20000
    assert p["direction"] == "upgrade" and p["amount_due_minor"] == 10000 and p["payment_required"] is True
    assert p["from_plan"] == "starter" and p["to_plan"] == "pro" and p["currency"] == "INR"
    assert p["renewal_date"] == "2026-02-01" and p["new_cycle_price_minor"] == 31000
    assert p["timezone"] == "Asia/Kolkata"


def test_preview_errors(frappe, env):
    sub = setup(frappe)
    from saas_core.billing import plan_change as pc
    for target, code in (("nope", "unknown_plan"), ("pro", "same_plan")):
        with pytest.raises(pc.PlanChangeError) as e:
            pc.preview(sub, target, now=NOON_JAN11)
        assert e.value.code == code and isinstance(e.value, ValueError)
    sub.period_end = None
    with pytest.raises(pc.PlanChangeError) as e:
        pc.preview(sub, "starter", now=NOON_JAN11)
    assert e.value.code == "no_period_end"


def test_price_missing_error(frappe, env):
    sub = setup(frappe)
    mk(frappe, "Plan", name="free", code="free", available=1, sort_order=0, prices=[])
    from saas_core.billing import plan_change as pc
    with pytest.raises(pc.PlanChangeError) as e:
        pc.preview(sub, "free", now=NOON_JAN11)
    assert e.value.code == "no_price"


def test_legacy_paid_fallback_to_latest_invoice(frappe, env):
    sub = setup(frappe, cur="starter", paid=0)
    mk(frappe, "Invoice", name="INV-1", subscription="SUB-1", status="paid", total_amount=15500,
       paid_at=dt.datetime(2026, 1, 1))
    from saas_core.billing import plan_change as pc
    assert pc.preview(sub, "pro", now=NOON_JAN11)["amount_due_minor"] == 10000


def test_upgrade_creates_link_and_waits(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    assert out["payment_required"] is True and out["scheduled"] is False
    assert out["payment_url"] == "https://rzp.io/i/x" and env.jobs == []
    link = env.gw.calls[0][1]
    assert link["amount"] == 10000 and link["notes"]["kind"] == "plan_change" and link["notes"]["tenant"] == "acme"
    assert sub.plan_change_state == "awaiting_payment" and sub.pending_plan == "pro"
    assert sub.plan_change_link_id == "plink_1" and sub.plan_change_amount == 10000
    assert sub.plan == "starter" and sub.paid_amount == 15500
    assert not any(c[0] == "update" for c in env.gw.calls)


def test_link_paid_schedules_once(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc
    pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    assert pc.on_link_paid(sub, NOON_JAN11) is True
    assert pc.on_link_paid(sub, NOON_JAN11) is False  # replay
    assert sub.plan_change_state == "scheduled" and sub.pending_plan == "pro"
    assert sub.plan_change_at == dt.datetime(2026, 1, 11, 18, 30)  # Jan 12 00:00 IST
    assert sub.paid_amount == 20000 and sub.period_start == sub.plan_change_at
    assert [c for c in env.gw.calls if c[0] == "update"] == [("update", "sub_1", "plan_pro", True)]
    assert len(env.jobs) == 1 and env.jobs[0][1] == "change_plan"
    k = env.jobs[0][2]
    assert k["args"]["plan"] == "pro" and k["args"]["from"] == "starter" and k["dedupe_key"] == "change_plan:acme:pro"
    assert [a for a, _ in env.audit] == ["subscription.plan_change"]


def test_processor_hook_settles_link(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc, processor
    pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    ev = frappe._Doc(invoice_ref="plink_1", gateway=RZ, amount=10000)
    processor._payment_succeeded(ev, {"payload": {}}, NOON_JAN11)
    processor._payment_succeeded(ev, {"payload": {}}, NOON_JAN11)
    assert sub.plan_change_state == "scheduled" and len(env.jobs) == 1
    assert sub.period_end == END and frappe.get_all("Invoice") == []


def test_downgrade_refunds_and_schedules(frappe, env):
    sub = setup(frappe)
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    # old share 20000, new share 10000 -> credit 10000
    assert out["scheduled"] is True and out["payment_required"] is False
    assert ("refund", "pay_1", 10000, out["refund"]["id"]) in env.gw.calls
    ref = frappe.get_all("Refund")[0]
    assert ref.kind == "plan_change" and ref.amount == 10000 and ref.status == "processed"
    assert sub.paid_amount == 10000 and sub.refunded_amount == 10000 and sub.plan_change_state == "scheduled"
    assert sub.plan == "pro"  # still on the old plan until the effective instant
    assert len(env.jobs) == 1


def test_refund_failure_still_schedules(frappe, env):
    sub = setup(frappe)
    env.gw.fail_refund = True
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    assert out["scheduled"] is True and out["refund"]["status"] == "failed"
    assert frappe.get_all("Refund")[0].status == "failed" and sub.get("refunded_amount") in (None, 0)


def test_gateway_update_failure_tolerated(frappe, env):
    sub = setup(frappe)
    env.gw.fail_update = True
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    assert out["scheduled"] is True and sub.plan_change_state == "scheduled" and len(env.jobs) == 1
    assert any(m[0] == "error" for m in frappe._store.messages)


def test_last_day_change_no_payment(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc
    last = dt.datetime(2026, 1, 31, 6, 30)
    p = pc.preview(sub, "pro", now=last)
    assert p["days_remaining"] == 0 and p["amount_due_minor"] == 0 and p["payment_required"] is False
    out = pc.apply(sub, "pro", "u@x.com", now=last)
    assert out["scheduled"] is True and not [c for c in env.gw.calls if c[0] == "link"]
    assert sub.plan_change_at == dt.datetime(2026, 1, 31, 18, 30)


def test_trial_schedules_without_payment(frappe, env):
    sub = setup(frappe, cur="starter", status="trialing", paid=0)
    from saas_core.billing import plan_change as pc
    assert pc.preview(sub, "pro", now=NOON_JAN11)["payment_required"] is False
    out = pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    assert out["scheduled"] is True and not [c for c in env.gw.calls if c[0] == "link"]


def test_sweep_switches_only_after_effective(frappe, env):
    sub = setup(frappe)
    from saas_core.billing import plan_change as pc
    pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    assert pc.sweep_due(dt.datetime(2026, 1, 11, 12, 0))["switched"] == 0 and sub.plan == "pro"
    out = pc.sweep_due(dt.datetime(2026, 1, 11, 18, 30))
    assert out["switched"] == 1 and sub.plan == "starter" and sub.plan_change_state == ""
    assert sub.pending_plan == "" and frappe.get_doc("Tenant", "acme").plan == "starter"
    assert pc.sweep_due(dt.datetime(2026, 1, 12))["switched"] == 0


def test_awaiting_payment_expires(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc
    pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    sub.modified = dt.datetime(2026, 1, 11, 6, 30)
    assert pc.sweep_due(dt.datetime(2026, 1, 11, 10, 0))["expired"] == 0
    assert pc.sweep_due(dt.datetime(2026, 1, 12, 7, 0))["expired"] == 1
    assert sub.plan_change_state == "" and sub.plan_change_link_id == "plink_1" and sub.plan == "starter"
    assert pc.on_link_paid(sub, None) is False


def _paid_ev(frappe, pay_id="pay_link", amount=10000):
    return frappe._Doc(invoice_ref="plink_1", gateway=RZ, amount=amount), {"payload": {"payment": {"entity": {
        "id": pay_id, "amount": amount}}, "payment_link": {"entity": {"id": "plink_1", "amount_paid": amount}}}}


def test_link_paid_records_extra_payment(frappe, env):
    import json
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc, processor
    pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    ev, p = _paid_ev(frappe)
    processor._payment_succeeded(ev, p, NOON_JAN11)
    processor._payment_succeeded(ev, p, NOON_JAN11)  # replay
    assert json.loads(sub.extra_payments) == [{"payment_id": "pay_link", "amount": 10000}]
    assert sub.paid_amount == 20000 and sub.plan_change_state == "scheduled"


def test_downgrade_after_upgrade_refund_split_never_exceeds_payment(frappe, env):
    import json
    sub = setup(frappe, cur="pro", paid=20000, extra_payments=json.dumps([{"payment_id": "pay_link", "amount": 10000}]))
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)  # credit 10000
    refunds = [c for c in env.gw.calls if c[0] == "refund"]
    credit = out["refund"]["amount_minor"]
    assert 0 < credit <= 10000 and [(c[1], c[2]) for c in refunds] == [("pay_link", credit)]
    assert out["refund"]["status"] == "processed" and sub.refunded_amount == credit
    assert json.loads(sub.extra_payments) == [{"payment_id": "pay_link", "amount": 10000 - credit}]


def test_downgrade_credit_exceeding_extra_spills_to_original(frappe, env):
    import json
    sub = setup(frappe, cur="pro", paid=31000, extra_payments=json.dumps([{"payment_id": "pay_link", "amount": 4000}]))
    from saas_core.billing import plan_change as pc
    pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)  # credit 10000
    assert [(c[1], c[2]) for c in env.gw.calls if c[0] == "refund"] == [("pay_link", 4000), ("pay_1", 6000)]
    assert len(frappe.get_all("Refund")) == 2


def test_downgrade_refund_reuses_existing_refund(frappe, env):
    sub = setup(frappe)
    from saas_core.billing import plan_change as pc
    pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    n = len([c for c in env.gw.calls if c[0] == "refund"])
    sub.pending_plan = "starter"
    again = pc._refund(sub, "u@x.com", 10000, "INR")
    assert again["amount_minor"] == 10000 and len(frappe.get_all("Refund")) == 1
    assert len([c for c in env.gw.calls if c[0] == "refund"]) == n


def test_pending_downgrade_refund_not_counted_until_processed(frappe, env):
    sub = setup(frappe)
    env.gw.refund_payment = lambda pid, amt, *, notes=None, receipt="": {"id": "rfnd_p", "status": "pending"}
    from saas_core.billing import plan_change as pc
    out = pc.apply(sub, "starter", "u@x.com", now=NOON_JAN11)
    assert out["refund"]["status"] == "pending" and not sub.get("refunded_amount")


def test_late_payment_of_expired_link_is_refunded(frappe, env):
    sub = setup(frappe, cur="starter", paid=15500)
    from saas_core.billing import plan_change as pc, processor
    pc.apply(sub, "pro", "u@x.com", now=NOON_JAN11)
    sub.modified = dt.datetime(2026, 1, 11, 6, 30)
    pc.sweep_due(dt.datetime(2026, 1, 12, 7, 0))
    ev, p = _paid_ev(frappe)
    processor._payment_succeeded(ev, p, dt.datetime(2026, 1, 12, 8, 0))
    processor._payment_succeeded(ev, p, dt.datetime(2026, 1, 12, 8, 0))  # replay
    refunds = [c for c in env.gw.calls if c[0] == "refund"]
    assert [(c[1], c[2]) for c in refunds] == [("pay_link", 10000)]
    rs = frappe.get_all("Refund")
    assert len(rs) == 1 and rs[0].amount == 10000 and rs[0].gateway_payment_id == "pay_link"
    assert sub.plan_change_state == "" and sub.paid_amount == 15500 and frappe.get_all("Invoice") == []
    assert sub.refunded_amount == 10000
