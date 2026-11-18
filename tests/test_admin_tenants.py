import sys
import types

import pytest


def seed(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


@pytest.fixture
def env(frappe, monkeypatch):
    seed(frappe, "Plan", name="starter", code="starter", template="erp")
    seed(frappe, "Template", name="erp")
    enq = []
    jobs = types.ModuleType("saas_core.jobs")
    jobs.enqueue_job = lambda t, k, **kw: enq.append((t, k)) or "PJ-1"
    audit = types.ModuleType("saas_core.audit")
    audit.log = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "saas_core.jobs", jobs)
    monkeypatch.setitem(sys.modules, "saas_core.audit", audit)
    frappe._roles = ["SaaS Admin"]
    frappe.session.user = "admin@x.com"
    from saas_core.api.v1.admin import tenants
    monkeypatch.setattr(tenants, "_send_invite", lambda e, title=None: True)
    return frappe, tenants, enq


def test_create(env):
    frappe, t, enq = env
    out = t.create(owner_email="Owner@X.com", slug="acme", plan="starter")
    assert out["tenant"]["status"] == "pending" and out["owner_created"] and out["invite_sent"]
    assert frappe.local.response["http_status_code"] == 202
    assert enq == [("acme", "provision")]
    assert frappe.db.exists("User", "owner@x.com")
    assert frappe.get_doc("Tenant", "acme").owner_user == "owner@x.com"
    assert frappe.db.exists("Subscription", {"owner_user": "owner@x.com", "status": "active"})


def test_create_existing_user_no_invite(env):
    frappe, t, enq = env
    seed(frappe, "User", name="o@x.com", email="o@x.com")
    out = t.create(owner_email="o@x.com", slug="acme", plan="starter")
    assert out["owner_created"] is False and out["invite_sent"] is False


def test_validation(env):
    frappe, t, _ = env
    assert t.create(owner_email="bad", slug="acme", plan="starter")["error"]["code"] == "validation_error"
    assert t.create(owner_email="o@x.com", slug="acme", plan="nope")["error"]["code"] == "validation_error"
    assert t.create(owner_email="o@x.com", plan="starter")["error"]["code"] == "bad_request"


def test_invite_and_list(env):
    frappe, t, _ = env
    t.create(owner_email="o@x.com", slug="acme", plan="starter")
    assert t.invite(tenant="acme") == {"tenant": "acme", "owner_email": "o@x.com", "invite_sent": True}
    assert t.invite(tenant="zzz")["error"]["code"] == "not_found"
    assert t.list()["items"][0]["owner_email"] == "o@x.com"


def test_customer_forbidden(env):
    frappe, t, _ = env
    frappe.session.user = "c@x.com"
    frappe._roles = ["SaaS Customer"]
    for call in (lambda: t.list(), lambda: t.create(owner_email="o@x.com", slug="acme", plan="starter"),
                 lambda: t.invite(tenant="x")):
        assert call()["error"]["code"] == "forbidden"


def test_guest_rejected(env):
    frappe, t, _ = env
    frappe.session.user = "Guest"
    with pytest.raises(frappe.PermissionError):
        t.list()


def test_send_invite_branded_and_default(frappe):
    import jinja2
    from saas_core.api.v1.admin import tenants
    calls, mails = [], []
    frappe.sendmail = lambda **k: mails.append(k)
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)

    class U(frappe._Doc):
        def reset_password(self, send_email=False):
            calls.append(send_email)
            return "https://x.io/update-password?key=K1"
    frappe._store.docs[("User", "o@x.com")] = U(doctype="User", name="o@x.com")
    frappe._store.docs[("Email Template", "INV")] = frappe._Doc(
        doctype="Email Template", name="INV", subject="Invite", response_html="<a href='{{ setup_url }}'>x</a>")
    assert tenants._send_invite("o@x.com") is True and calls == [True] and mails == []
    frappe.conf["saas_core_admin_invite_email_template"] = "INV"
    assert tenants._send_invite("o@x.com") is True and calls == [True, False]
    assert len(mails) == 1 and "key=K1" in mails[0]["message"]


def test_admin_invite_links_use_console_routes(frappe):
    from saas_core.api.v1.admin import tenants
    mails = []
    frappe.sendmail = lambda **k: mails.append(k)
    frappe.utils.get_url = lambda: "https://cp.example.com"
    frappe._store.docs[("User", "o@x.com")] = frappe._Doc(doctype="User", name="o@x.com")  # no reset_password -> fallback mail
    assert tenants._send_invite("o@x.com") is True
    assert "https://cp.example.com/console/forgot-password" in mails[-1]["message"]
    assert "cp.example.com/login" not in mails[-1]["message"]
    frappe.conf["saas_core_console_url"] = "/portal"
    tenants._send_invite("o@x.com")
    assert "https://cp.example.com/portal/forgot-password" in mails[-1]["message"]
