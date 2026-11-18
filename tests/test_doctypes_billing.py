"""Billing DocType JSON + controller tests (pure python)."""
import json, pathlib, importlib
import pytest

ROOT = pathlib.Path(__file__).parent.parent / "saas_core" / "saas_core" / "doctype"
EXPECT = {
 "plan": "code title description is_public available trial_days fc_server_plan limits template features prices sort_order",
 "plan_price": "interval currency amount gateway gateway_price_id",
 "subscription": "tenant owner_user plan gateway gateway_sub_id gateway_customer_id interval currency status period_end trial_end cancel_at_period_end dunning_stage first_failure_at next_retry_at suspend_date last_event_at payment_mocked billing_mode period_start paid_amount gateway_payment_id refunded_amount canceled_at cancel_effective_at cancel_reason pending_plan plan_change_state plan_change_at plan_change_link_id plan_change_amount extra_payments",
 "invoice": "tenant owner_user subscription gateway gateway_invoice_id number status total_amount currency issued_at paid_at period_start period_end pdf_url payment_mocked gateway_payment_id",
 "refund": "subscription tenant owner_user invoice gateway gateway_payment_id gateway_refund_id amount currency kind status reason days_used days_unused error requested_at processed_at",
 "payment_event": "event_key gateway event_id kind status raw_payload subscription_ref tenant_ref error processed_at next_retry_at attempts",
 "checkout_session": "session_key gateway session_ref tenant owner_user plan interval currency billing_mode checkout_mode amount_minor gateway_price_id price_row subscription consumed_at",
 "gateway_account": "account_name gateway mode checkout_mode enabled key_id key_secret webhook_secret extra",
}

def load(n): return json.loads((ROOT / n / f"{n}.json").read_text())

@pytest.mark.parametrize("n", EXPECT)
def test_json(n):
    d = load(n)
    for k in ("doctype", "name", "module", "fields", "permissions", "engine"):
        assert k in d
    assert d["module"] == "Saas Core"
    assert [f["fieldname"] for f in d["fields"]] == EXPECT[n].split()
    assert (ROOT / n / "__init__.py").exists()
    roles = {p["role"] for p in d["permissions"]}
    assert "SaaS Customer" not in roles and "Guest" not in roles
    if not d.get("istable"):
        assert {"SaaS Admin", "System Manager"} <= roles

def test_special():
    assert load("plan")["autoname"] == "field:code"
    pe = load("payment_event")
    assert pe["autoname"] == "field:event_key"
    assert [f for f in pe["fields"] if f["fieldname"] == "event_key"][0]["unique"] == 1
    ga = {f["fieldname"]: f for f in load("gateway_account")["fields"]}
    assert ga["key_secret"]["fieldtype"] == ga["webhook_secret"]["fieldtype"] == "Password"
    assert load("plan_price")["istable"] == 1

def mk(frappe, mod, cls, **kw):
    m = importlib.import_module(f"saas_core.saas_core.doctype.{mod}.{mod}")
    return getattr(m, cls.replace(" ", ""))(doctype=cls, **kw)

def test_plan(frappe):
    p = mk(frappe, "plan", "Plan", prices=[{"interval": "month", "currency": "USD", "amount": 0, "gateway": "stripe"}])
    p.validate()
    p["prices"].append({"interval": "month", "currency": "USD", "amount": 5, "gateway": "stripe"})
    with pytest.raises(frappe.ValidationError): p.validate()
    p["prices"][1].update(interval="year")
    p.validate()
    p["prices"][1]["amount"] = -1
    with pytest.raises(frappe.ValidationError): p.validate()

def test_subscription(frappe):
    s = mk(frappe, "subscription", "Subscription", status="active")
    s.validate()
    s["status"] = "bogus"
    with pytest.raises(frappe.ValidationError): s.validate()
    s["status"] = "active"
    s["get_doc_before_save"] = lambda: {"status": "canceled"}
    with pytest.raises(frappe.ValidationError): s.validate()
    s["status"] = "canceled"
    s.validate()

def test_payment_event(frappe):
    e = mk(frappe, "payment_event", "Payment Event", event_key="s:1", status="received", attempts=0)
    e.validate()
    e["get_doc_before_save"] = lambda: {"event_key": "s:1", "status": "received", "attempts": 0}
    e["status"] = "processed"; e["attempts"] = 1; e.validate()
    e["event_key"] = "s:2"
    with pytest.raises(frappe.ValidationError): e.validate()

def test_gateway(frappe):
    g = mk(frappe, "gateway_account", "Gateway Account", mode="test")
    g.validate()
    g["mode"] = "prod"
    with pytest.raises(frappe.ValidationError): g.validate()
