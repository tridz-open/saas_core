import pytest


def _mod():
    from saas_core.api.v1 import auth
    return auth


def test_signup_disabled(frappe):
    frappe.session.user = "Guest"
    frappe.conf["saas_core_public_signup"] = 0
    out = _mod().signup(email="a@b.co", password="longpass123")
    assert out["error"]["code"] == "signup_disabled"
    assert frappe.local.response["http_status_code"] == 403
    assert _mod().config() == {"public_signup": False, "signup_mode": "closed", "payment_banner": None, "site_domain": None}


@pytest.mark.parametrize("v", [None, 1, "1", True])
def test_flag_enabled_values(frappe, v):
    if v is not None:
        frappe.conf["saas_core_public_signup"] = v
    assert _mod().public_signup_enabled() is True


@pytest.mark.parametrize("v", [0, "0", "false", False])
def test_flag_disabled_values(frappe, v):
    frappe.conf["saas_core_public_signup"] = v
    assert _mod().public_signup_enabled() is False


@pytest.mark.parametrize("mode,exp", [("closed", False), ("invite", True), ("allowlist", True), ("open", True)])
def test_config_reflects_signup_mode(frappe, mode, exp):
    frappe.conf["saas_core_signup_mode"] = mode
    out = _mod().config()
    assert out["signup_mode"] == mode and out["public_signup"] is exp
    frappe.conf["saas_core_public_signup"] = 0
    assert _mod().config()["public_signup"] is False


def test_config_exposes_site_domain(frappe):
    frappe.conf["saas_core_site_domain"] = ".apps.example.com"
    assert _mod().config()["site_domain"] == "apps.example.com"
