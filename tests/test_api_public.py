"""Public/session endpoints: plans, templates, signup, login, logout, me, password reset."""
import sys
import types

import pytest


def seed(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


def fake_auth(frappe, fail=False):
    mod = types.ModuleType("frappe.auth")

    class LoginManager:
        def authenticate(self, user=None, pwd=None):
            if fail or not frappe.db.exists("User", user):
                raise frappe.AuthenticationError("bad")
            self.user = user

        def post_login(self):
            frappe.session.user = self.user

        def logout(self):
            frappe.session.user = "Guest"
            frappe._store.messages.append(("logout", (), {}))
    mod.LoginManager = LoginManager
    sys.modules["frappe.auth"] = mod
    frappe.auth = mod


def test_plans_list_public_guest(frappe):
    frappe.session.user = "Guest"
    seed(frappe, "Plan", name="pro", code="pro", title="Pro", is_public=1, available=1, sort_order=1,
         limits='{"sites":1}', features='["a"]', trial_days=7,
         prices=[{"interval": "month", "amount": 900, "currency": "USD", "gateway": "stripe", "gateway_price_id": "price_secret"}])
    seed(frappe, "Plan", name="hid", code="hid", title="H", is_public=0, prices=[])
    from saas_core.api.v1 import plans
    out = plans.list()
    assert [p["code"] for p in out["items"]] == ["pro"] and out["next_cursor"] is None
    assert out["items"][0]["prices"] == [{"interval": "month", "amount": 900, "currency": "USD"}]
    assert "price_secret" not in str(out)
    assert plans.list(currency="EUR")["items"] == []
    assert plans.list(interval="bad")["error"]["code"] == "bad_request"


def test_plans_capacity_closed(frappe):
    ent = types.ModuleType("saas_core.billing.entitlements"); ent.capacity_open = lambda: False
    sys.modules["saas_core.billing.entitlements"] = ent
    seed(frappe, "Plan", name="pro", code="pro", title="Pro", is_public=1, available=1, prices=[])
    from saas_core.api.v1 import plans
    assert plans.list()["items"][0]["available"] is False


def test_templates_list(frappe):
    seed(frappe, "Template", name="blank", slug="blank", title="Blank", is_public=1)
    seed(frappe, "Template", name="priv", slug="priv", title="P", is_public=0)
    from saas_core.api.v1 import templates
    out = templates.list(limit=10)
    assert [t["slug"] for t in out["items"]] == ["blank"]
    assert templates.list(limit=0)["error"]["code"] == "bad_request"


def test_signup_creates_customer_201(frappe):
    fake_auth(frappe)
    frappe.conf["saas_core_signup_mode"] = "open"
    frappe.session.user = "Guest"
    frappe.form_dict.update(email="New@X.io", password="longpass123", full_name="New")
    from saas_core.api.v1 import auth
    out = auth.signup()
    assert out["email"] == "new@x.io" and out["user"] == "new@x.io"
    assert frappe.local.response["http_status_code"] == 201
    u = frappe.get_doc("User", "new@x.io")
    assert u["roles"] == [{"role": "SaaS Customer"}]
    assert "longpass123" not in str(out)


def test_signup_validation_and_conflict(frappe):
    fake_auth(frappe)
    frappe.conf["saas_core_signup_mode"] = "open"
    frappe.session.user = "Guest"
    from saas_core.api.v1 import auth
    r = auth.signup(email="bad", password="short")
    assert r["error"]["code"] == "validation_error" and set(r["error"]["field_errors"]) == {"email", "password"}
    assert frappe.local.response["http_status_code"] == 422
    seed(frappe, "User", name="a@x.io", email="a@x.io")
    r = auth.signup(email="a@x.io", password="longpass123")
    assert r["error"]["code"] == "conflict" and frappe.local.response["http_status_code"] == 409
    assert auth.signup()["error"]["code"] == "bad_request"


def test_signup_idempotent_and_mismatch(frappe):
    fake_auth(frappe)
    frappe.conf["saas_core_signup_mode"] = "open"
    frappe.session.user = "Guest"
    frappe.form_dict["idempotency_key"] = "k1"
    from saas_core.api.v1 import auth
    a = auth.signup(email="i@x.io", password="longpass123")
    frappe.session.user = "Guest"
    b = auth.signup(email="i@x.io", password="longpass123")
    assert a == b and "error" not in b
    r = auth.signup(email="other@x.io", password="longpass123")
    assert r["error"]["code"] == "idempotency_mismatch" and frappe.local.response["http_status_code"] == 409


def test_signup_rate_limited(frappe):
    fake_auth(frappe)
    frappe.conf["saas_core_signup_mode"] = "open"
    frappe.session.user = "Guest"
    from saas_core.api.v1 import auth
    for i in range(10):
        auth.signup(email=f"u{i}@x.io", password="longpass123")
    frappe.session.user = "Guest"
    r = auth.signup(email="z@x.io", password="longpass123")
    assert r["error"]["code"] == "rate_limited" and frappe.local.response["http_status_code"] == 429


def test_login_ok_and_bad(frappe):
    seed(frappe, "User", name="a@x.io", email="a@x.io", full_name="A")
    frappe.session.user = "Guest"
    fake_auth(frappe)
    from saas_core.api.v1 import auth
    me = auth.login(email="a@x.io", password="whatever1234")
    assert me["user"] == "a@x.io" and "pwd" not in str(me)
    fake_auth(frappe, fail=True)
    r = auth.login(email="a@x.io", password="nope")
    assert r["error"]["code"] == "unauthorized" and frappe.local.response["http_status_code"] == 401


def test_me_requires_login_and_returns_counts(frappe):
    from saas_core.api.v1 import auth
    frappe.session.user = "Guest"
    with pytest.raises(frappe.PermissionError):
        auth.me()
    seed(frappe, "User", name="a@x.io", email="a@x.io", full_name="A")
    seed(frappe, "Tenant", name="acme", owner_user="a@x.io")
    frappe._roles = ["SaaS Customer"]
    frappe.session.user = "a@x.io"
    me = auth.me()
    assert me["tenant_count"] == 1 and me["roles"] == ["SaaS Customer"] and me["full_name"] == "A"


def test_logout(frappe):
    fake_auth(frappe)
    from saas_core.api.v1 import auth
    frappe.session.user = "Guest"
    with pytest.raises(frappe.PermissionError):
        auth.logout()
    frappe.session.user = "a@x.io"
    assert auth.logout() is None and frappe.local.response["http_status_code"] == 204


def test_password_reset_request_no_enumeration(frappe):
    from saas_core.api.v1 import password_reset as pr
    frappe.session.user = "Guest"
    sent = []

    class U(frappe._Doc):
        def reset_password(self, send_email=False): sent.append(self["name"])
    frappe._store.docs[("User", "a@x.io")] = U(doctype="User", name="a@x.io", enabled=1)
    assert pr.request(email="a@x.io") is None and frappe.local.response["http_status_code"] == 202
    assert pr.request(email="ghost@x.io") is None and frappe.local.response["http_status_code"] == 202
    assert sent == ["a@x.io"]


def test_password_reset_confirm(frappe):
    from saas_core.api.v1 import password_reset as pr
    frappe.session.user = "Guest"
    mod = types.ModuleType("frappe.core.doctype.user.user")
    seen = []

    def update_password(new_password, key=None, **k):
        if key != "good":
            raise Exception("invalid")
        seen.append(new_password)
    mod.update_password = update_password
    for n in ("frappe.core", "frappe.core.doctype", "frappe.core.doctype.user"):
        sys.modules.setdefault(n, types.ModuleType(n))
    sys.modules["frappe.core.doctype.user.user"] = mod
    assert pr.confirm(key="good", new_password="longpass123") is None
    assert frappe.local.response["http_status_code"] == 204 and seen == ["longpass123"]
    assert pr.confirm(key="bad", new_password="longpass123")["error"]["code"] == "invalid_token"
    assert pr.confirm(key="good", new_password="short")["error"]["code"] == "validation_error"


def _branded_user(frappe, calls):
    class U(frappe._Doc):
        def reset_password(self, send_email=False):
            calls.append(send_email)
            return "https://x.io/update-password?key=SECRETKEY"
    frappe._store.docs[("User", "a@x.io")] = U(doctype="User", name="a@x.io", enabled=1)
    frappe._store.docs[("Email Template", "PW")] = frappe._Doc(
        doctype="Email Template", name="PW", subject="Reset {{ brand }}", response_html="<a href='{{ reset_url }}'>go</a>")


def test_password_reset_branded_template(frappe):
    import jinja2
    from saas_core.api.v1 import password_reset as pr
    frappe.session.user = "Guest"
    mails, calls = [], []
    frappe.sendmail = lambda **k: mails.append(k)
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)
    frappe.conf["saas_core_password_reset_email_template"] = "PW"
    _branded_user(frappe, calls)
    assert pr.request(email="a@x.io") is None
    assert calls == [False] and len(mails) == 1
    assert "key=SECRETKEY" in mails[0]["message"] and mails[0]["recipients"] == ["a@x.io"]


def test_password_reset_default_when_no_template(frappe):
    from saas_core.api.v1 import password_reset as pr
    frappe.session.user = "Guest"
    calls = []
    frappe.sendmail = lambda **k: calls.append("mail")
    _branded_user(frappe, calls)
    pr.request(email="a@x.io")
    assert calls == [True]
