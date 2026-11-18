import pytest


@pytest.fixture
def T(frappe):
    from saas_core.notifications import templating
    frappe.utils.get_url = lambda *a, **k: "https://control.test"
    return templating


def test_absolute_url(T):
    assert T.absolute_url("/assets/x.png") == "https://control.test/assets/x.png"
    assert T.absolute_url("https://cdn/x.png") == "https://cdn/x.png"
    assert T.absolute_url("") == ""


def test_absolutize_only_url_keys(T):
    out = T.absolutize({"logo_url": "/a.png", "billing_url": "/console/billing", "login_url": "https://s/login", "brand": "/x"})
    assert out == {"logo_url": "https://control.test/a.png", "billing_url": "https://control.test/console/billing",
                   "login_url": "https://s/login", "brand": "/x"}


def test_brand_context_absolutizes(T, monkeypatch):
    monkeypatch.setattr(T, "get_setting", lambda k, d=None: "/assets/acme_saas_recipe/brand/acme-logo.png" if k == "logo_url" else d)
    ctx = T.brand_context(billing_url="/console/billing")
    assert ctx["logo_url"] == "https://control.test/assets/acme_saas_recipe/brand/acme-logo.png"
    assert ctx["billing_url"] == "https://control.test/console/billing"
