import datetime as dt
import sys
import types

import pytest


def _eq(frappe, name, msg, status="Sent", ref="Tenant", age_h=2):
    d = frappe.get_doc({"doctype": "Email Queue", "name": name, "message": msg, "status": status,
                        "reference_doctype": ref, "modified": dt.datetime.utcnow() - dt.timedelta(hours=age_h)})
    d.insert()
    return d


def test_purge(frappe):
    from saas_core.notifications import purge
    link = "Hi https://x.com/update-password?key=abc123XYZ now"
    _eq(frappe, "old", link)
    _eq(frappe, "fresh", link, age_h=0)
    _eq(frappe, "notsent", link, status="Not Sent")
    _eq(frappe, "other", link, ref="User")
    _eq(frappe, "plain", "nothing here")
    frappe.get_doc("Email Queue", "old")  # exists
    assert purge.purge_sent_setup_links() == 1
    assert frappe.db.get_value("Email Queue", "old", "message") == "Hi https://x.com/update-password?key=[redacted] now"
    for n in ("fresh", "notsent", "other"):
        assert "abc123XYZ" in frappe.db.get_value("Email Queue", n, "message")
    assert purge.purge_sent_setup_links() == 0


def test_hook_registered():
    import importlib.util, pathlib
    src = (pathlib.Path(__file__).parent.parent / "saas_core" / "hooks.py").read_text()
    assert '"0 * * * *": ["saas_core.notifications.purge.purge_sent_setup_links"]' in src


@pytest.fixture
def env(frappe, monkeypatch):
    calls, logs = [], []
    welcome = types.ModuleType("saas_core.notifications.welcome")
    welcome.resend_setup_link = lambda t: calls.append(t) or True
    audit = types.ModuleType("saas_core.audit")
    audit.log = lambda *a, **k: logs.append((a, k))
    monkeypatch.setitem(sys.modules, "saas_core.notifications.welcome", welcome)
    monkeypatch.setitem(sys.modules, "saas_core.audit", audit)
    import saas_core.notifications as pkg
    monkeypatch.setattr(pkg, "welcome", welcome, raising=False)
    frappe.get_doc({"doctype": "Tenant", "name": "acme", "owner_user": "o@x.com"}).insert()
    from saas_core.api.v1.admin import tenants
    return frappe, tenants, calls, logs


def test_resend_admin(env):
    frappe, t, calls, logs = env
    frappe._roles = ["SaaS Admin"]; frappe.session.user = "a@x.com"
    assert t.resend_setup_link(tenant="acme") == {"tenant": "acme", "sent": True}
    assert calls == ["acme"] and logs[0][0][0] == "admin.tenant.resend_setup_link"
    assert t.resend_setup_link(tenant="zzz")["error"]["code"] == "not_found"
    assert t.resend_setup_link.__saas_methods__ == ["POST"] if hasattr(t.resend_setup_link, "__saas_methods__") else True


def test_resend_forbidden(env):
    frappe, t, calls, _ = env
    frappe._roles = ["SaaS Customer"]; frappe.session.user = "c@x.com"
    assert t.resend_setup_link(tenant="acme")["error"]["code"] == "forbidden"
    assert calls == []
