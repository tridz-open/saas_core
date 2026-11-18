import importlib
import pytest

SECRET = "sk_unit_secret"


class FakeHttp:
    def __init__(self, fail_currency=None):
        self.calls, self.fail_currency, self.n = [], fail_currency, 0
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        item = kw["json_body"]["item"]
        if item["currency"] == self.fail_currency:
            raise RuntimeError(f"400 currency not supported key={SECRET} rzp_test_abc123")
        self.n += 1
        return {"id": f"plan_{self.n}"}


def setup(frappe, mode="test", prices=None):
    acc = frappe._Doc(doctype="Gateway Account", name="rzp", account_name="rzp", gateway="razorpay",
                      mode=mode, enabled=1, key_id="rzp_test_abc123")
    acc.get_password = lambda f, raise_exception=True: SECRET
    acc.insert()
    plan = frappe._Doc(doctype="Plan", name="pro", title="Pro", prices=[
        dict(p) for p in (prices or [
            {"interval": "month", "currency": "INR", "amount": 49900, "gateway": "razorpay"},
            {"interval": "year", "currency": "INR", "amount": 499000, "gateway": ""},
            {"interval": "month", "currency": "INR", "amount": 100, "gateway": "stripe"},
            {"interval": "month", "currency": "USD", "amount": 900, "gateway": "razorpay", "gateway_price_id": "plan_old"},
        ])])
    plan.insert()
    return plan


def mod():
    return importlib.import_module("saas_core.gateways.razorpay_sync")


def test_sync_creates_plans_and_saves_ids(frappe, capsys):
    plan = setup(frappe)
    http = FakeHttp()
    res = mod().sync_plans(http=http)
    assert [s["plan_id"] for s in res["synced"]] == ["plan_1", "plan_2"] and res["skipped"] == 1
    m, url, kw = http.calls[0]
    assert (m, url.endswith("/v1/plans"), kw["basic_auth"]) == ("POST", True, ("rzp_test_abc123", SECRET))
    assert kw["json_body"]["period"] == "monthly" and kw["json_body"]["interval"] == 1
    assert kw["json_body"]["item"]["amount"] == 49900 and kw["json_body"]["item"]["currency"] == "INR"
    assert http.calls[1][2]["json_body"]["period"] == "yearly"
    rows = plan["prices"]
    assert rows[0]["gateway_price_id"] == "plan_1" and rows[1]["gateway_price_id"] == "plan_2"
    assert rows[1]["gateway"] == "razorpay" and not rows[2].get("gateway_price_id") and rows[3]["gateway_price_id"] == "plan_old"
    out = capsys.readouterr().out
    assert "plan_1" in out and SECRET not in out and "rzp_test" not in out


def test_second_run_is_noop(frappe):
    setup(frappe)
    mod().sync_plans(http=FakeHttp())
    http = FakeHttp()
    res = mod().sync_plans(http=http)
    assert http.calls == [] and res["synced"] == []


def test_api_error_recorded_without_secrets_and_continues(frappe, capsys):
    plan = setup(frappe, prices=[
        {"interval": "month", "currency": "USD", "amount": 900, "gateway": "razorpay"},
        {"interval": "month", "currency": "INR", "amount": 49900, "gateway": "razorpay"}])
    res = mod().sync_plans(http=FakeHttp(fail_currency="USD"))
    assert len(res["errors"]) == 1 and res["errors"][0]["currency"] == "USD"
    assert "currency not supported" in res["errors"][0]["error"]
    assert SECRET not in res["errors"][0]["error"] and "rzp_test" not in res["errors"][0]["error"]
    assert plan["prices"][1]["gateway_price_id"] == "plan_1" and not plan["prices"][0].get("gateway_price_id")
    out = capsys.readouterr().out
    assert SECRET not in out and "rzp_test" not in out


def test_live_account_refused_unless_live(frappe):
    setup(frappe, mode="live")
    http = FakeHttp()
    with pytest.raises(frappe.ValidationError):
        mod().sync_plans(http=http)
    assert http.calls == []
    assert len(mod().sync_plans(live=True, http=http)["synced"]) == 2


def test_explicit_account_must_be_razorpay(frappe):
    setup(frappe)
    frappe._Doc(doctype="Gateway Account", name="st", gateway="stripe", mode="test", enabled=1).insert()
    with pytest.raises(frappe.ValidationError):
        mod().sync_plans(account="st", http=FakeHttp())
