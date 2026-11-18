import json, pathlib
import jinja2
import pytest

FX = pathlib.Path(__file__).parent.parent / "saas_core/fixtures/email_template.json"
URL = "https://acme.example.com/update-password?key=SECRETKEY"


@pytest.fixture
def env(frappe, monkeypatch):
    st = frappe._store
    from saas_core.provisioning import runner

    class Port:
        n = 0
        def create_password_setup_link(self, site, user="Administrator"):
            Port.n += 1
            return f"https://{site}/update-password?key=SECRETKEY{Port.n}"
    monkeypatch.setattr(runner, "_port", Port())
    sent = []
    frappe.sendmail = lambda **kw: sent.append(kw)
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)

    class T(frappe._Doc):
        def get_password(self, f, raise_exception=True): return "S3cretPw!"  # must never be read/rendered
    for tpl in json.loads(FX.read_text()):
        st.docs[("Email Template", tpl["name"])] = frappe._Doc(tpl)
    st.docs[("User", "o@x.com")] = frappe._Doc(doctype="User", name="o@x.com", email="o@x.com")
    st.docs[("Tenant", "acme")] = T(doctype="Tenant", name="acme", slug="acme", owner_user="o@x.com",
                                    fc_site="acme.example.com", plan="Pro", title="Acme Cafe")
    return frappe, sent


# ---- email 1: "Complete your setup" -------------------------------------------------------------------------
def test_setup_mail_sends_once_with_link_only(env):
    frappe, sent = env
    from saas_core.notifications.welcome import send_tenant_setup
    assert send_tenant_setup("acme") is True
    assert len(sent) == 1
    m = sent[0]
    assert m["recipients"] == ["o@x.com"] and m["reference_doctype"] == "Tenant" and m["reference_name"] == "acme"
    assert m["now"] is False
    assert f'href="{URL}1"' in m["message"] and "Set your password &amp; continue" in m["message"]
    assert "Complete your setup" in m["message"] and "Complete your" in m["subject"] and "Acme Cafe" in m["subject"]
    assert "works once and expires in 24 hours" in m["message"]
    assert "S3cretPw!" not in m["message"] and "Temporary password" not in m["message"]
    t = frappe._store.docs[("Tenant", "acme")]
    assert t["setup_email_sent_at"] and not t.get("welcome_sent_at")      # welcome is NOT sent yet
    assert send_tenant_setup("acme") is False
    assert len(sent) == 1


def test_setup_template_override_and_branding(env):
    frappe, sent = env
    frappe._store.conf = None
    frappe.conf["saas_core_setup_email_template"] = "custom"
    frappe.conf["saas_core_brand"] = "Acme"
    frappe._store.docs[("Email Template", "custom")] = frappe._Doc(doctype="Email Template", name="custom",
                                                                   subject="Hi {{ brand }}", response_html="<p>{{ site_url }}</p>")
    from saas_core.notifications.welcome import send_tenant_setup
    send_tenant_setup("acme")
    assert sent[0]["subject"] == "Hi Acme" and sent[0]["message"] == "<p>https://acme.example.com</p>"


def test_setup_url_not_logged_or_stored(env):
    frappe, sent = env
    from saas_core.notifications.welcome import send_tenant_setup
    send_tenant_setup("acme")
    send_tenant_setup("acme")
    assert "SECRETKEY" not in repr(frappe._store.messages)
    assert "SECRETKEY" not in repr(frappe._store.docs[("Tenant", "acme")])


def test_without_setup_url_falls_back_to_login(env, monkeypatch):
    frappe, sent = env
    from saas_core.provisioning import runner
    monkeypatch.setattr(runner, "_port", object())  # no create_password_setup_link support
    from saas_core.notifications.welcome import send_tenant_setup
    send_tenant_setup("acme")
    assert "https://acme.example.com/login" in sent[0]["message"] and "S3cretPw!" not in sent[0]["message"]


def test_setup_url_param_removed_and_old_name_gone():
    import inspect
    from saas_core.notifications import welcome
    assert "setup_url" not in inspect.signature(welcome.send_tenant_setup).parameters
    assert not hasattr(welcome, "send_tenant_ready")


def test_resend_setup_link_bypasses_guard_with_new_link(env):
    frappe, sent = env
    from saas_core.notifications.welcome import send_tenant_setup, resend_setup_link
    assert send_tenant_setup("acme") is True
    assert send_tenant_setup("acme") is False
    assert resend_setup_link("acme") is True
    assert len(sent) == 2
    assert "SECRETKEY1" in sent[0]["message"] and "SECRETKEY2" in sent[1]["message"]


def test_setup_mail_dedupe_is_locked(env, monkeypatch):
    frappe, sent = env
    from saas_core import audit
    from saas_core.notifications import welcome
    seen = []
    real = audit.cache_lock
    monkeypatch.setattr(audit, "cache_lock", lambda k, **kw: (seen.append(k), real(k, **kw))[1])
    welcome.send_tenant_setup("acme")
    assert "saas_core:setup_mail:acme" in seen


# ---- email 2: welcome, only after setup_complete ----------------------------------------------------------------
def test_welcome_not_sent_before_setup_complete(env):
    frappe, sent = env
    from saas_core.notifications.welcome import send_tenant_welcome
    assert send_tenant_welcome("acme") is False and not sent


def test_welcome_has_site_url_next_steps_and_no_link_key(env):
    frappe, sent = env
    frappe._store.docs[("Tenant", "acme")]["setup_completed_at"] = "2026-11-17 10:00:00"
    from saas_core.notifications.welcome import send_tenant_welcome
    assert send_tenant_welcome("acme") is True
    m = sent[0]
    assert m["recipients"] == ["o@x.com"] and m["now"] is False and "Acme Cafe" in m["subject"]
    assert "https://acme.example.com" in m["message"] and "Next steps" in m["message"]
    assert "update-password" not in m["message"] and "key=" not in m["message"] and "SECRETKEY" not in m["message"]
    assert "S3cretPw!" not in m["message"]
    t = frappe._store.docs[("Tenant", "acme")]
    assert t["welcome_sent_at"]
    assert send_tenant_welcome("acme") is False and len(sent) == 1
    # a welcome never mints a link
    from saas_core.provisioning import runner
    assert runner.get_port().n == 0


def test_welcome_template_override_key(env):
    frappe, sent = env
    frappe._store.docs[("Tenant", "acme")]["setup_completed_at"] = "2026-11-17"
    frappe.conf["saas_core_welcome_email_template"] = "w2"
    frappe._store.docs[("Email Template", "w2")] = frappe._Doc(doctype="Email Template", name="w2", subject="W {{ brand }}", response_html="x")
    from saas_core.notifications.welcome import send_tenant_welcome
    send_tenant_welcome("acme")
    assert sent[0]["subject"].startswith("W ")


def test_fixture_templates_present():
    names = {t["name"] for t in json.loads(FX.read_text())}
    assert names == {"saas-tenant-setup", "saas-tenant-ready", "saas-invoice-paid", "saas-payment-failed", "saas-dunning", "saas-beta-invite",
                     "saas-cancellation", "saas-refund-processed", "saas-renewal-receipt"}
    from saas_core import hooks
    assert {"saas-tenant-setup", "saas-tenant-ready"} <= set(hooks.fixtures[2]["filters"][0][2])
