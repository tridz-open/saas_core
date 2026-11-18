import pytest


def test_request_and_confirm(frappe, monkeypatch):
    from saas_core.api.v1 import verify_email as ve
    frappe.session.user = "a@x.io"
    frappe._store.docs[("User", "a@x.io")] = frappe._Doc(doctype="User", name="a@x.io", email="a@x.io")
    mails = []
    frappe.sendmail = lambda **k: mails.append(k)
    frappe.utils = type("U", (), {"get_url": staticmethod(lambda: "https://h")})
    assert ve.request() is None and frappe.local.response["http_status_code"] == 202
    key = mails[0]["message"].split("key=")[1]
    assert mails[0]["recipients"] == ["a@x.io"]
    assert ve.confirm(key="nope")["error"]["code"] == "invalid_token"
    assert ve.confirm()["error"]["code"] == "bad_request"
    assert ve.confirm(key=key) is None and frappe.local.response["http_status_code"] == 204
    assert frappe._store.docs[("User", "a@x.io")].get("email_verified") == 1
    assert ve.confirm(key=key)["error"]["code"] == "invalid_token"


def test_requires_login_and_user_bound(frappe):
    from saas_core.api.v1 import verify_email as ve
    frappe.session.user = "Guest"
    with pytest.raises(frappe.PermissionError):
        ve.request()
    with pytest.raises(frappe.PermissionError):
        ve.confirm(key="k")
    frappe.session.user = "a@x.io"
    frappe.cache().set_value("saas_core:verify_email:k", "b@x.io")
    assert ve.confirm(key="k")["error"]["code"] == "invalid_token"
