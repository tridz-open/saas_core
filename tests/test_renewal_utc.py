"""issue_due_invoices compares period_end (naive UTC) against UTC, not the site-local clock (Asia/Kolkata, +5:30)."""
import datetime as dt, sys, types
import pytest


class FakeGw:
    def create_payment_link(self, **kw):
        return {"id": "plink_1", "short_url": "https://rzp.io/i/x"}


@pytest.fixture
def env(frappe, monkeypatch):
    from saas_core.gateways import registry
    monkeypatch.setattr(registry, "get_gateway", lambda name, *a, **k: FakeGw())
    # site timezone Asia/Kolkata: Frappe's now_datetime() is naive IST
    monkeypatch.setattr(sys.modules["frappe.utils"], "now_datetime",
                        lambda: dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30))
    return frappe


def _sub(frappe, period_end):
    mk = lambda d, **kw: frappe.get_doc({"doctype": d, **kw}).insert()
    mk("Plan", name="pro", code="pro", available=1,
       prices=[{"interval": "month", "currency": "INR", "amount": 49900, "gateway": "razorpay"}])
    mk("Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")
    return mk("Subscription", tenant="acme", owner_user="u@x.com", plan="pro", status="active", interval="month",
              currency="INR", gateway="razorpay", billing_mode="invoice", period_end=period_end)


def _utc():
    return dt.datetime.utcnow()


def test_not_due_in_utc_is_not_issued_when_local_clock_is_ahead(env):
    from saas_core.billing import processor
    _sub(env, _utc() + dt.timedelta(hours=2))  # due in 2h UTC, but already "past" by the IST wall clock
    assert processor.issue_due_invoices() == 0
    assert env.get_all("Invoice") == []


def test_due_in_utc_is_issued_and_stamped_utc(env):
    from saas_core.billing import processor
    _sub(env, _utc() - dt.timedelta(minutes=1))
    assert processor.issue_due_invoices() == 1
    inv = env.get_doc("Invoice", env.get_all("Invoice", pluck="name")[0])
    assert abs((inv.get("issued_at") - _utc()).total_seconds()) < 120  # UTC, not IST
