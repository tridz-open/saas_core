import jinja2
import pytest


@pytest.fixture
def env(frappe):
    st = frappe._store
    sent = []
    frappe.sendmail = lambda **kw: sent.append(kw)
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)
    st.docs[("User", "o@x.com")] = frappe._Doc(doctype="User", name="o@x.com", email="o@x.com")
    st.docs[("Tenant", "acme")] = frappe._Doc(doctype="Tenant", name="acme", title="Acme")
    st.docs[("Subscription", "S1")] = frappe._Doc(doctype="Subscription", name="S1", tenant="acme", owner_user="o@x.com")
    st.docs[("Invoice", "INV1")] = frappe._Doc(doctype="Invoice", name="INV1", number="INV-1", tenant="acme", owner_user="o@x.com",
                                               subscription="S1", total_amount=1250, currency="usd")
    return frappe, sent


def test_amount_format():
    from saas_core.notifications.billing_mail import format_amount
    assert format_amount(1250, "usd") == "$12.50"
    assert format_amount(500, "JPY") == "500 JPY"
    assert format_amount(1040000, "INR") == "₹10,400"
    assert format_amount(10400000, "INR") == "₹1,04,000"
    assert format_amount(123450, "INR") == "₹1,234.50"


def test_payment_failed_fallback_and_idempotent(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    assert bm.send_payment_failed("INV1", "card declined") is True
    assert bm.send_payment_failed("INV1", "card declined") is False
    assert len(sent) == 1
    assert sent[0]["recipients"] == ["o@x.com"]
    assert "$12.50" in sent[0]["message"] and "card declined" in sent[0]["message"]
    assert "/console/billing" in sent[0]["message"]


def test_template_used_and_billing_url_setting(env):
    frappe, sent = env
    frappe.conf["saas_core_console_url"] = "https://c.example.com"
    frappe.conf["saas_core_invoice_email_template"] = "inv"
    frappe._store.docs[("Email Template", "inv")] = frappe._Doc(
        doctype="Email Template", name="inv", subject="Invoice {{ invoice_number }}",
        response_html="<p>{{ amount }} {{ billing_url }} {{ tenant_title }}</p>")
    from saas_core.notifications import billing_mail as bm
    assert bm.send_invoice_paid("INV1") is True
    assert sent[0]["subject"] == "Invoice INV-1"
    assert sent[0]["message"] == "<p>$12.50 https://c.example.com/billing Acme</p>"
    assert bm.send_invoice_paid("INV1") is False


def test_dunning_per_attempt_idempotent(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    assert bm.send_dunning("S1", 1) and not bm.send_dunning("S1", 1)
    assert bm.send_dunning("S1", 2)
    assert len(sent) == 2


def test_no_recipient_skips(env):
    frappe, sent = env
    frappe._store.docs[("Invoice", "INV1")]["owner_user"] = None
    from saas_core.notifications import billing_mail as bm
    assert bm.send_invoice_paid("INV1") is False and sent == []


def test_invoice_url_is_console_invoice_route(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    assert bm.send_invoice_paid("INV1") is True
    assert "/console/billing/invoices/INV1" in sent[0]["message"]


def test_dedupe_runs_under_lock_and_marker_set_inside(env, monkeypatch):
    frappe, sent = env
    from saas_core import audit
    from saas_core.notifications import billing_mail as bm
    seen = []
    real = audit.cache_lock
    monkeypatch.setattr(audit, "cache_lock", lambda k, **kw: (seen.append(k), real(k, **kw))[1])
    assert bm.send_payment_failed("INV1") is True
    assert any(k.startswith("saas_core:billing_mail:payment_failed:INV1") for k in seen)
    assert bm.send_payment_failed("INV1") is False
    assert len(sent) == 1


def test_failed_send_does_not_consume_marker(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    def boom(**kw): raise RuntimeError("smtp")
    frappe.sendmail = boom
    with pytest.raises(RuntimeError):
        bm.send_payment_failed("INV1")
    frappe.sendmail = lambda **kw: sent.append(kw)
    assert bm.send_payment_failed("INV1") is True


def test_receipt_dates_render_in_site_timezone(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail
    import datetime as dt
    inv = frappe._store.docs[("Invoice", "INV1")]
    inv.paid_at = dt.datetime(2026, 11, 30, 20, 30)  # naive UTC = 1 Dec 02:00 IST
    inv.period_start = dt.datetime(2026, 11, 30, 18, 30)  # 00:00 IST on 1 Dec
    inv.period_end = dt.datetime(2026, 12, 31, 18, 30)
    from saas_core.notifications import templating
    assert templating.fmt_date(inv.paid_at, utc=True) == "1 Dec 2026"
    assert templating.fmt_date(inv.paid_at) == "30 Nov 2026"
    assert templating.fmt_date("2026-11-30T20:30:00Z", utc=True) == "1 Dec 2026"
    frappe._store.docs[("Email Template", "saas-invoice-paid")] = frappe._Doc(
        doctype="Email Template", name="saas-invoice-paid", subject="x", response_html="{{ paid_date }}|{{ period }}", response="")
    assert billing_mail.send_invoice_paid("INV1") is True
    assert sent[-1]["message"] == "1 Dec 2026|1 Dec 2026 to 1 Jan 2027"
