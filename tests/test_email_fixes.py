"""Email audit fixes: absolute console links, display titles (never raw ids), formatted dates, no blank variables."""
import jinja2
import pytest


@pytest.fixture
def env(frappe):
    st = frappe._store
    sent = []
    frappe.sendmail = lambda **kw: sent.append(kw)
    frappe.conf["saas_core_brand"] = "Acme"
    frappe.conf["saas_core_plan_code_prefix"] = "acme-"
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)
    frappe.utils.now_datetime = lambda: __import__("datetime").datetime(2026, 11, 17, 9, 0)
    st.docs[("User", "o@x.com")] = frappe._Doc(doctype="User", name="o@x.com", email="o@x.com", full_name="Asha Nair")
    st.docs[("Tenant", "e2e123456")] = frappe._Doc(doctype="Tenant", name="e2e123456", slug="e2e123456", title="")
    st.docs[("Tenant", "acme")] = frappe._Doc(doctype="Tenant", name="acme", title="Acme Cafe")
    st.docs[("Subscription", "S1")] = frappe._Doc(doctype="Subscription", name="S1", tenant="acme", owner_user="o@x.com",
                                                  plan="acme-standard", next_retry_at="2026-11-20 10:30:00", suspend_date="2026-11-27")
    st.docs[("Invoice", "INV1")] = frappe._Doc(doctype="Invoice", name="INV1", number="INV-1", tenant="acme", owner_user="o@x.com",
                                               subscription="S1", total_amount=536800, currency="inr", status="unpaid",
                                               period_start="2026-11-17 00:00:00", period_end="2026-12-16 18:30:00")
    return frappe, sent


def test_fmt_date_and_titles(env):
    from saas_core.notifications import templating
    assert templating.fmt_date("2026-12-16 18:30:00") == "16 Dec 2026"
    assert templating.fmt_date("") == "" and templating.fmt_date("garbage") == ""
    assert templating.display_title("e2e123456") == "your restaurant"       # never the id / slug
    assert templating.display_title("acme") == "Acme Cafe"
    assert templating.plan_label("acme-standard") == "Standard"


def test_invoice_is_a_receipt_with_formatted_dates(env, monkeypatch):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    frappe._store.docs[("Email Template", "saas-invoice-paid")] = frappe._Doc(doctype="Email Template", name="saas-invoice-paid", subject="x", response_html="{{ tenant_title }}|{{ paid_date }}|{{ period }}|{{ plan_label }}|{{ invoice_url }}", response="")
    import datetime as _d
    from saas_core.billing import proration
    monkeypatch.setattr(proration, "naive_utc_now", lambda now=None: _d.datetime(2026, 11, 17, 3, 30))  # fallback "today" is UTC
    assert bm.send_invoice_paid("INV1") is True
    body = sent[0]["message"]
    # period_end 16 Dec 18:30 UTC is 00:00 IST on 17 Dec: dates render in the site timezone
    assert body.startswith("Acme Cafe|17 Nov 2026|17 Nov 2026 to 17 Dec 2026|Standard|") and body.endswith("/console/billing/invoices/INV1")
    assert "00:00" not in body and "18:30" not in body
    assert sent[0]["subject"] == "x"


def test_dunning_provides_amount_and_formatted_dates(env):
    frappe, sent = env
    from saas_core.notifications import templating
    from saas_core.notifications import billing_mail as bm
    captured = {}
    orig = templating.render
    templating_render = lambda key, ctx: captured.update(ctx) or None
    import saas_core.notifications.billing_mail as m
    m.templating.render = templating_render
    try:
        assert bm.send_dunning("S1", attempt=3) is True
    finally:
        m.templating.render = orig
    assert captured["amount"] == "₹5,368" and captured["is_final"] is True
    assert captured["next_retry"] == "20 Nov 2026" and captured["suspend_date"] == "27 Nov 2026"
    assert captured["tenant_title"] == "Acme Cafe"


def test_verify_and_invite_links_are_absolute_console_routes(env, monkeypatch):
    frappe, sent = env
    frappe.utils.get_url = lambda: "https://cp.example.com"
    from saas_core.api.v1 import verify_email as ve
    from saas_core.api.v1.tenants import team
    ve._send("a@x.io", "KEY1")
    assert sent[-1]["message"].endswith("https://cp.example.com/console/verify-email?key=KEY1")
    t = frappe._store.docs[("Tenant", "acme")]
    team._send_invite("b@x.io", t, "TOK", "o@x.com", "admin")
    m = sent[-1]
    assert m["subject"] == "Asha Nair invited you to Acme Cafe on Acme"
    assert "https://cp.example.com/console/accept-invite?token=TOK" in m["message"] and "as admin" in m["message"]
    team._send_invite("b@x.io", frappe._store.docs[("Tenant", "e2e123456")], "TOK2", "o@x.com", "member")
    assert "e2e123456" not in sent[-1]["subject"] + sent[-1]["message"]


def test_setup_mail_fallback_is_honest(env, monkeypatch):
    frappe, sent = env
    frappe._store.docs[("Tenant", "acme")].owner_user = "o@x.com"
    from saas_core.notifications import welcome
    ctx = welcome._context(frappe._store.docs[("Tenant", "acme")], "acme.example.com")
    assert ctx["admin_user"] != "Administrator" and ctx["tenant_title"] == "Acme Cafe"
    assert ctx["site_url_display"] == "acme.example.com" and ctx["plan_label"] == ""
