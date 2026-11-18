"""Regenerates saas_core/fixtures/email_template.json (all saas-* templates) from the shared layout, plus previews/*.html.
usage: python scripts/build_email_templates.py"""
import json
import pathlib
import sys

from jinja2 import Environment

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "saas_core" / "notifications"))
from email_layout import MONO, box, btn, h, num, p, paste, row, rows, shell  # noqa: E402

FIX = ROOT / "saas_core" / "fixtures"
CTA = "{{ brand_color|default('#AA1845') }}"
URL = "{{ cta_url }}"


def B(url, label):
    return btn(url, label, CTA)


def policy():
    return '<p class="mu" style="margin:16px 0 12px;font-size:13px;line-height:20px;color:#6B7280">{{ policy_line }}</p>'


def sitebox(title):
    return box(f"<strong>{title}</strong><br><span style=\"font-family:{MONO}\"><a href=\"{{{{ site_url }}}}\" style=\"color:inherit;text-decoration:none\">"
               "{{ site_url_display|default(site_url) }}</a></span>{% if owner_email %}<br>Admin account: {{ owner_email }}{% endif %}"
               "{% if plan %}<br>Plan: {{ plan_label|default(plan) }}{% endif %}")


def steps():
    items = [("Add your menu", "Create items, prices and categories for your guests."), ("Invite your team", "Add staff accounts with the right roles."),
             ("Take your first order", "Open the point of sale and ring up a test order.")]
    r = "".join(f'<tr><td valign="top" width="28" class="tx" style="padding:6px 0;font-size:14px;color:#0D0D0F">{i}.</td><td class="bd" style="padding:6px 0;font-size:14px;line-height:21px;color:#4B5563">'
                f'<strong class="tx" style="color:#0D0D0F">{a}</strong><br>{b}</td></tr>' for i, (a, b) in enumerate(items, 1))
    return '<p class="tx" style="margin:20px 0 4px;font-weight:600;color:#0D0D0F">Next steps</p><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">' + r + '</table>'


T = {}
T["saas-tenant-setup"] = ("Complete your {{ brand }} setup for {{ tenant_title }}", "Set your password to finish setting up {{ tenant_title }}.",
    h("Complete your setup") + p("<strong>{{ tenant_title }}</strong> has been created. Set your password below, sign in, then add your details to finish setup. It takes about two minutes.")
    + B("{{ setup_url }}", "{% if setup_is_link|default(true) %}Set your password &amp; continue{% else %}Go to sign in{% endif %}") + paste("{{ setup_url }}") + sitebox("Your site")
    + p("{% if setup_is_link|default(true) %}This link works once and expires in {{ setup_link_hours|default(24) }} hours. If it has expired, use <strong>Forgot password</strong> on <a href=\"{{ login_url|default(site_url ~ '/login') }}\" style=\"color:inherit\">{{ login_url|default(site_url ~ '/login') }}</a>."
        "{% else %}Choose <strong>Forgot password</strong> on the sign-in page to set your password.{% endif %}"))
T["saas-tenant-ready"] = ("Welcome to {{ brand }}: {{ tenant_title }} is ready", "Setup is complete. {{ tenant_title }} is ready to use.",
    h("Welcome, your workspace is ready") + p("Setup is complete. <strong>{{ tenant_title }}</strong> is ready to use. Sign in any time at the address below.")
    + B("{{ login_url }}", "Open your workspace") + paste("{{ login_url }}") + sitebox("Your workspace") + steps()
    + p("Forgot your password? Use <strong>Forgot password</strong> on the sign-in page."))
T["saas-invoice-paid"] = ("Payment received for {{ tenant_title or brand }}: invoice {{ invoice_number }}", "Thanks, your payment of {{ amount }} was received.",
    h("Payment received") + p("Thanks! We received your payment for <strong>{{ tenant_title }}</strong>. Your receipt is below.")
    + rows(row("Invoice", "{{ invoice_number }}", "invoice_number"), row("Amount paid", num("{{ amount }}"), "amount"),
           row("Workspace", "{{ tenant_title }}", "tenant_title"), row("Period", "{{ period }}", "period"))
    + B(URL, "View receipt") + paste(URL) + policy())
T["saas-payment-failed"] = ("Action needed: your {{ brand }} payment failed", "{% if amount %}Payment of {{ amount }} failed.{% else %}Your payment failed.{% endif %} Update your payment method.",
    h("Your payment didn't go through") + p("We couldn't charge your payment method for <strong>{{ tenant_title }}</strong>. Update it to keep your workspace running.")
    + rows(row("Invoice", "{{ invoice_number }}", "invoice_number"), row("Amount", num("{{ amount }}"), "amount"), row("Reason", "{{ reason }}", "reason"),
           row("Next retry", "{{ next_retry }}", "next_retry"))
    + box("{% if suspend_date %}Your workspace will be paused on <strong>{{ suspend_date }}</strong> if payment isn't completed.{% else %}We'll retry automatically, but updating your card now avoids interruption.{% endif %}")
    + B(URL, "Update payment method") + paste(URL) + policy())
T["saas-dunning"] = ("Reminder: your {{ brand }} payment is overdue", "Payment overdue for {{ tenant_title }}. Update billing to avoid interruption.",
    h("Your payment is overdue") + p("This is reminder {{ attempt }} that payment for <strong>{{ tenant_title }}</strong> is overdue.")
    + rows(row("Workspace", "{{ tenant_title }}", "tenant_title"), row("Amount due", num("{{ amount }}"), "amount"), row("Next retry", "{{ next_retry }}", "next_retry"),
           row("Pause date", "{{ suspend_date }}", "suspend_date"))
    + box("Update your billing details to avoid your workspace being paused.") + B(URL, "Update billing") + paste(URL) + policy())
T["saas-beta-invite"] = ("You're invited to try {{ brand }}", "You have been invited to try {{ brand }}. Create your account to get started.",
    h("You're invited to try {{ brand }}") + p("You've been added to the early-access list. Create your account with <strong>{{ email }}</strong> to get started.")
    + B("{{ signup_url }}", "Create your account") + paste("{{ signup_url }}")
    + p("Use this email address when you sign up: the invite only works for it."))
T["saas-cancellation"] = ("Your subscription is canceled{% if refund_amount %}: {{ refund_amount }} refund on the way{% endif %}",
    "Your subscription for {{ tenant_title }} is canceled. Site stays active until {{ stops_at_text }}.",
    h("Subscription canceled") + p("Your subscription for <strong>{{ tenant_title }}</strong> has been canceled. Your site stays active until {{ stops_at_text }}.")
    + rows(row("Restaurant", "{{ tenant_title }}", "tenant_title"), row("Plan", "{{ plan_label }}", "plan_label"), row("Site active until", "{{ stops_at_text }}", "stops_at_text"),
           row("Days used", "{{ days_used }}", "days_used is defined and days_used != ''"), row("Charged for days used", num("{{ charge_amount }}"), "charge_amount"),
           row("Refund to original payment method", num("{{ refund_amount }}"), "has_refund and refund_amount"))
    + "{% if has_refund %}" + p("Refunds usually take 5 to 7 business days to show up on your statement.") + "{% endif %}"
    + B(URL, "View billing") + paste(URL) + policy())
T["saas-refund-processed"] = ("Your refund of {{ refund_amount }} has been processed", "Your refund of {{ refund_amount }} has been processed.",
    h("Refund processed") + p("We have processed your refund for <strong>{{ tenant_title }}</strong>.")
    + rows(row("Refund amount", num("{{ refund_amount }}"), "refund_amount"), row("Refunded to", "{{ payment_method }}", "payment_method"), row("Date", "{{ refund_date }}", "refund_date"),
           row("Reference", "{{ reference }}", "reference"), row("Restaurant", "{{ tenant_title }}", "tenant_title"))
    + p("Refunds usually take 5 to 7 business days to show up on your statement.") + B(URL, "View billing") + paste(URL) + policy())
T["saas-renewal-receipt"] = ("Renewal receipt for {{ tenant_title or brand }}: {{ amount }} paid", "Your subscription renewed. We received {{ amount }}.",
    h("Renewal receipt") + p("Your subscription for <strong>{{ tenant_title }}</strong> has renewed. Your receipt is below.")
    + rows(row("Invoice", "{{ invoice_number }}", "invoice_number"), row("Amount paid", num("{{ amount }}"), "amount"), row("Plan", "{{ plan_label }}", "plan_label"),
           row("Period", "{{ period }}", "period"), row("Next renewal on", "{{ next_renewal_date }}", "next_renewal_date"))
    + p("If a renewal payment fails, we retry it and send you reminders before your site is paused.") + B(URL, "View receipt") + paste(URL) + policy())

SAMPLE = dict(brand="Acme Cloud", brand_color="#AA1845", support_email="support@example.com", logo_url="https://example.com/assets/brand/logo-light@2x.png",
    logo_url_dark="https://example.com/assets/brand/logo-dark@2x.png", tenant_title="Cafe Aroma", setup_url="https://cafe-aroma.example.com/update-password?key=abc",
    setup_is_link=True, setup_link_hours=24, site_url="https://cafe-aroma.example.com", site_url_display="cafe-aroma.example.com", login_url="https://cafe-aroma.example.com/login",
    owner_email="owner@example.com", plan="growth", plan_label="Standard", invoice_number="INV-0042", amount="INR 4,999.00", period="17 Nov 2026 to 16 Dec 2026",
    cta_url="https://app.example.com/billing", policy_line="Billed monthly in advance. If you cancel, we charge up to the end of that day and refund the unused days.",
    reason="Card declined", next_retry="20 Nov 2026", suspend_date="25 Nov 2026", attempt=2, signup_url="https://app.example.com/signup", email="owner@example.com",
    stops_at_text="18 Nov 2026, 11:59 PM IST", days_used=12, charge_amount="INR 1,600.00", refund_amount="INR 3,399.00", has_refund=True, payment_method="Visa ending 4242",
    refund_date="18 Nov 2026", reference="rfnd_0042", next_renewal_date="17 Dec 2026")

if __name__ == "__main__":
    docs = []
    (FIX / "previews").mkdir(exist_ok=True)
    for name, (subj, pre, body) in T.items():
        html = shell(subj, pre, body)
        docs.append({"doctype": "Email Template", "name": name, "subject": subj, "use_html": 1, "response_html": html})
        (FIX / "previews" / f"{name}.html").write_text(Environment().from_string(html).render(**SAMPLE))
    (FIX / "email_template.json").write_text(json.dumps(docs, indent=1) + "\n")
    print(len(docs), "templates")
