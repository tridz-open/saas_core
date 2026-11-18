import pytest

TID, SEC, WH = "rzp_test_" + "abc123", "sekret-value-1", "whsec-value-1"


def conf(frappe, **kw):
    frappe.conf.update({"saas_core_razorpay_key_id": TID, "saas_core_razorpay_key_secret": SEC,
                        "saas_core_razorpay_webhook_secret": WH, **kw})


def test_create_then_update(frappe, capsys):
    conf(frappe)
    from saas_core.gateways.bootstrap import from_site_config
    r = from_site_config()
    assert r["action"] == "created"
    d = frappe.get_doc("Gateway Account", r["account"])
    assert (d.gateway, d.mode, d.key_id, d.key_secret, d.webhook_secret) == ("razorpay", "test", TID, SEC, WH)
    conf(frappe, saas_core_razorpay_key_secret="new-secret")
    r2 = from_site_config()
    assert r2 == {"account": r["account"], "action": "updated"}
    assert frappe.db.count("Gateway Account") == 1
    assert frappe.get_doc("Gateway Account", r["account"]).key_secret == "new-secret"
    out = capsys.readouterr()
    assert SEC not in out.out + out.err and WH not in out.out + out.err
    for _, a, k in frappe._store.messages:
        assert SEC not in repr((a, k)) and WH not in repr((a, k))


def test_live_key_refused_unless_allowed(frappe):
    conf(frappe, saas_core_razorpay_key_id="rzp_live_" + "zzz")
    from saas_core.gateways.bootstrap import from_site_config
    with pytest.raises(Exception) as e:
        from_site_config()
    assert "zzz" not in str(e.value) and SEC not in str(e.value)
    assert frappe.db.count("Gateway Account") == 0
    assert from_site_config(allow_live=True)["action"] == "created"


def test_missing_keys_error_has_no_values(frappe):
    frappe.conf.update({"saas_core_razorpay_key_id": TID})
    from saas_core.gateways.bootstrap import from_site_config
    with pytest.raises(Exception) as e:
        from_site_config()
    assert "key_secret" in str(e.value) and TID not in str(e.value)
