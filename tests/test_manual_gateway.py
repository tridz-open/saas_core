import sys, types, pytest
from test_billing_flow import mk, jobs  # noqa: F401  (jobs fixture)


def setup_t(frappe):
    mk(frappe, "Plan", name="pro", code="pro", available=1, trial_days=0)
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="pending", plan="pro")


def test_manual_checkout_active_and_mocked(frappe, jobs):
    setup_t(frappe)
    mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="test", enabled=1)
    from saas_core.gateways.manual import ManualGateway
    from saas_core.gateways.base import CheckoutParams
    gw = ManualGateway()
    res = gw.create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="x",
                                                    success_url="https://ok", metadata={"interval": "month"}))
    assert res.url == "https://ok" and res.raw["payment_mocked"] == 1
    sub = frappe.get_all("Subscription")[0]
    assert sub.status == "active" and sub.payment_mocked == 1 and sub.gateway == "manual"
    inv = frappe.get_all("Invoice")[0]
    assert inv.status == "paid" and inv.payment_mocked == 1
    assert ("acme", "provision", "provision:acme") in jobs
    from saas_core.api.v1.serializers import to_subscription, to_invoice
    assert to_subscription(sub)["payment_mocked"] is True and to_invoice(inv)["payment_mocked"] is True


def test_manual_live_mode_refused(frappe):
    from saas_core.saas_core.doctype.gateway_account.gateway_account import GatewayAccount
    acc = GatewayAccount(); acc.update({"gateway": "manual", "mode": "live"})
    with pytest.raises(Exception, match="live"):
        acc.validate()
    ok = GatewayAccount(); ok.update({"gateway": "manual", "mode": "test"}); ok.validate()
    other = GatewayAccount(); other.update({"gateway": "stripe", "mode": "live"}); other.validate()


def test_registry_refuses_live_manual(frappe):
    n = mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="live", enabled=1)["name"]
    from saas_core.gateways.registry import get_gateway
    with pytest.raises(Exception, match="live"):
        get_gateway("manual", account=n)


def test_banner(frappe):
    from saas_core.gateways.manual import payment_banner
    assert payment_banner() is None
    mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="test", enabled=1)
    assert payment_banner() == "Test mode: payments mocked"


def test_banner_follows_default_gateway(frappe):
    from saas_core.gateways.manual import payment_banner
    mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="test", enabled=1)
    rz = mk(frappe, "Gateway Account", account_name="r", gateway="razorpay", mode="test", enabled=1)["name"]
    frappe.conf["saas_core_default_gateway"] = "razorpay"
    assert payment_banner() == "Test mode — use a test card, no real charge"
    frappe.conf["saas_core_default_gateway"] = "manual"
    assert payment_banner() == "Test mode: payments mocked"
    frappe.db.set_value("Gateway Account", rz, "mode", "live")
    frappe.conf["saas_core_default_gateway"] = "razorpay"
    assert payment_banner() is None


def test_manual_checkout_stamps_naive_utc(frappe, jobs):
    """period_end / last_event_at / invoice issued_at/paid_at are naive UTC even on an IST site clock."""
    import datetime as dt
    setup_t(frappe)
    mk(frappe, "Gateway Account", account_name="m", gateway="manual", mode="test", enabled=1)
    from saas_core.gateways.manual import ManualGateway
    from saas_core.gateways.base import CheckoutParams
    before = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    ManualGateway().create_checkout_session(CheckoutParams(tenant="acme", plan="pro", gateway_price_id="x",
                                                           success_url="https://ok", metadata={"interval": "month"}))
    after = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    sub, inv = frappe.get_all("Subscription")[0], frappe.get_all("Invoice")[0]
    assert before <= sub.last_event_at <= after
    assert before <= inv.issued_at <= after and before <= inv.paid_at <= after
    assert before + dt.timedelta(days=30) <= sub.period_end <= after + dt.timedelta(days=30)
