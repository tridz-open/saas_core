import datetime as dt
import json
import pathlib

import jinja2
import pytest

FX = pathlib.Path(__file__).parent.parent / "saas_core/fixtures/email_template.json"
POLICY = "Billed monthly in advance. If you cancel, we charge up to the end of that day and refund the unused days."
NAMES = ["saas-invoice-paid", "saas-payment-failed", "saas-dunning", "saas-cancellation", "saas-refund-processed", "saas-renewal-receipt"]


@pytest.fixture
def env(frappe):
    st = frappe._store
    sent = []
    frappe.sendmail = lambda **kw: sent.append(kw)
    frappe.render_template = lambda s, ctx: jinja2.Template(s).render(**ctx)
    st.docs[("User", "o@x.com")] = frappe._Doc(doctype="User", name="o@x.com", email="o@x.com")
    st.docs[("Tenant", "acme")] = frappe._Doc(doctype="Tenant", name="acme", title="Acme Cafe")
    st.docs[("Subscription", "S1")] = frappe._Doc(
        doctype="Subscription", name="S1", tenant="acme", owner_user="o@x.com", plan="acme-standard", currency="INR",
        paid_amount=100000, cancel_effective_at=dt.datetime(2026, 11, 18, 18, 30), period_end=dt.datetime(2026, 12, 17))
    st.docs[("Invoice", "INV1")] = frappe._Doc(doctype="Invoice", name="INV1", number="INV-1", tenant="acme", owner_user="o@x.com",
                                               subscription="S1", total_amount=100000, currency="INR")
    st.docs[("Refund", "R1")] = frappe._Doc(doctype="Refund", name="R1", subscription="S1", tenant="acme", owner_user="o@x.com",
                                            amount=70000, currency="INR", days_used=9, gateway="razorpay", gateway_refund_id="rfnd_1",
                                            processed_at=dt.datetime(2026, 11, 19, 3, 0))
    return frappe, sent


def load(frappe):
    for t in json.loads(FX.read_text()):
        frappe._store.docs[("Email Template", t["name"])] = frappe._Doc(t)


def test_policy_in_all_six_templates():
    by = {t["name"]: t for t in json.loads(FX.read_text())}
    for n in NAMES:
        assert "{{ policy_line }}" in by[n]["response_html"], n
    from saas_core.notifications.templating import DEFAULT_TEMPLATES as D
    assert D["cancellation"] == "saas-cancellation" and D["refund_processed"] == "saas-refund-processed" and D["renewal_receipt"] == "saas-renewal-receipt"


def test_all_billing_mails_carry_policy(env):
    frappe, sent = env
    load(frappe)
    from saas_core.notifications import billing_mail as bm
    assert bm.BILLING_POLICY_LINE == POLICY
    bm.send_payment_failed("INV1", "declined"); bm.send_dunning("S1", 1); bm.send_invoice_paid("INV1")
    bm.send_cancellation("S1", "R1"); bm.send_refund_processed("R1"); bm.send_renewal_receipt("INV1")
    assert len(sent) == 6
    for m in sent:
        assert POLICY in m["message"], m["subject"]


def test_cancellation_render_and_refund_row(env):
    frappe, sent = env
    load(frappe)
    from saas_core.notifications import billing_mail as bm
    assert bm.send_cancellation("S1", "R1") is True
    m = sent[0]
    assert m["subject"] == "Your subscription is canceled: ₹700 refund on the way"
    assert "Acme Cafe" in m["message"] and "11:59 pm IST on 18 Nov 2026" in m["message"]
    assert "₹700" in m["message"] and "₹300" in m["message"] and ">9<" in m["message"] and "5 to 7 business days" in m["message"]
    assert m["reference_doctype"] == "Subscription" and m["recipients"] == ["o@x.com"]
    assert bm.send_cancellation("S1", "R1") is False and len(sent) == 1


def test_cancellation_without_refund_hides_row(env):
    frappe, sent = env
    load(frappe)
    from saas_core.notifications import billing_mail as bm
    assert bm.send_cancellation("S1") is True
    m = sent[0]
    assert m["subject"] == "Your subscription is canceled"
    assert "Refund to original payment method" not in m["message"] and "5 to 7 business days" not in m["message"]
    assert POLICY in m["message"]


def test_refund_processed(env):
    frappe, sent = env
    load(frappe)
    from saas_core.notifications import billing_mail as bm
    assert bm.send_refund_processed("R1") is True and bm.send_refund_processed("R1") is False
    m = sent[0]
    assert "₹700" in m["message"] and "rfnd_1" in m["message"] and "19 Nov 2026" in m["message"] and "Original payment method" in m["message"]
    assert m["reference_doctype"] == "Refund" and m["recipients"] == ["o@x.com"]


def test_renewal_receipt(env):
    frappe, sent = env
    load(frappe)
    from saas_core.notifications import billing_mail as bm
    assert bm.send_renewal_receipt("INV1") is True and bm.send_renewal_receipt("INV1") is False
    m = sent[0]
    assert m["subject"] == "Renewal receipt for Acme Cafe: ₹1,000 paid"
    assert "Next renewal on" in m["message"] and "17 Dec 2026" in m["message"] and "/console/billing/invoices/INV1" in m["message"]
    assert "we retry it and send you reminders before your site is paused" in m["message"]


def test_recipient_fallback_and_missing(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    frappe._store.docs[("Subscription", "S2")] = frappe._Doc(doctype="Subscription", name="S2", tenant="acme", owner_user="b@x.com")
    assert bm.send_cancellation("S2") is True and sent[0]["recipients"] == ["b@x.com"]
    frappe._store.docs[("Subscription", "S3")] = frappe._Doc(doctype="Subscription", name="S3", tenant="acme", owner_user="")
    assert bm.send_cancellation("S3") is False and len(sent) == 1


def test_plain_text_fallback_when_template_missing(env):
    frappe, sent = env
    from saas_core.notifications import billing_mail as bm
    assert bm.send_cancellation("S1", "R1") and bm.send_refund_processed("R1") and bm.send_renewal_receipt("INV1")
    assert len(sent) == 3
    for m in sent:
        assert m["message"].startswith("<p>") and POLICY in m["message"]
    assert "5 to 7 business days" in sent[0]["message"] and "₹700" in sent[1]["message"] and "retry" in sent[2]["message"]


def test_hooks_schedule_both_sweeps():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "saas_core" / "hooks.py").read_text()
    assert "billing.cancellation.sweep_due" in src and "billing.plan_change.sweep_due" in src
