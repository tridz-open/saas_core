"""Tenant onboarding emails, in two steps.

1. "Complete your setup" (`send_tenant_setup`, template `saas-tenant-setup`, config key `setup_email_template`):
   sent when provisioning finishes. Carries a one-time, expiring password-setup link (never the password). The link
   is minted inside the job via the provisioning port, rendered into the mail body once, and is never passed as a
   job argument or stored by us (the outgoing Email Queue row is the only place it lands).
2. Welcome (`send_tenant_welcome`, template `saas-tenant-ready`, config key `welcome_email_template`): sent only
   after the tenant site reports setup complete (tenants.setup_complete). Site URL and next steps, no link key.
"""
import frappe
from frappe.utils import now_datetime

from saas_core import audit
from saas_core.config import get_setting
from saas_core.notifications.templating import absolutize, display_title, host_display, plan_label

SETUP_TEMPLATE = "saas-tenant-setup"
DEFAULT_TEMPLATE = "saas-tenant-ready"   # the welcome (after setup)


def _recipient(tenant) -> str | None:
    user = tenant.get("owner_user")
    if not user:
        return None
    email = frappe.db.get_value("User", user, "email")
    return email or (user if "@" in str(user) else None)


def _mint_link(doc, site):
    """Fresh one-time setup link from the provisioning port; None if unsupported/failing (mail falls back)."""
    try:
        from saas_core.provisioning import runner
        return runner.get_port().create_password_setup_link(site)
    except Exception as e:  # noqa: BLE001 - never include secrets
        audit.log("tenant.admin_setup_link_failed", target_doctype="Tenant", target=doc.name,
                  details={"error": type(e).__name__})
        return None


def _context(doc, site):
    # tenant URL template: "https://{site}" on Frappe Cloud; local real-site mode uses "http://{site}:8004"
    site_url = str(get_setting("tenant_url_template", "https://{site}")).format(site=site)
    return {
        "brand": get_setting("brand", "Your Platform"),
        "brand_color": get_setting("brand_color", "#4f46e5"),
        "logo_url": get_setting("logo_url", ""),
        "logo_url_dark": get_setting("logo_url_dark", ""),
        "tenant_title": display_title(doc),   # the restaurant name; never the slug or tenant id
        "login_url": f"{site_url}/login",
        "site_url": site_url,
        "site_url_display": host_display(site_url),
        # the site account is the admin account created for the customer's signup email (not the word "Administrator")
        "owner_email": _recipient(doc) or "",
        "admin_user": "your admin account",
        "plan": plan_label(doc.get("plan")),
        "plan_label": plan_label(doc.get("plan")),
        "support_email": get_setting("support_email", "support@example.com"),
    }


def _mail(doc, template_key, default_template, ctx):
    to = _recipient(doc)
    if not to:
        frappe.log_error(f"onboarding email skipped: tenant {doc.name} has no owner email", "saas_core welcome")
        return False
    tpl = frappe.get_doc("Email Template", get_setting(template_key, default_template) or default_template)
    ctx = absolutize(ctx)
    subject = frappe.render_template(tpl.subject, ctx)
    message = frappe.render_template(tpl.response_html, ctx)
    frappe.sendmail(recipients=[to], subject=subject, message=message,
                    reference_doctype="Tenant", reference_name=doc.name, now=False)
    return True


def _site(doc):
    return doc.get("fc_site") or doc.get("site") or doc.get("slug") or doc.name


def _send_setup(doc):
    site = _site(doc)
    ctx = _context(doc, site)
    setup_url = _mint_link(doc, site)
    # No minted link: send the customer to the real sign-in page (Forgot password) and do not promise a one-time link.
    ctx["setup_is_link"] = bool(setup_url)
    ctx["setup_url"] = setup_url or ctx["login_url"]
    ctx["setup_link_hours"] = int(get_setting("setup_link_expiry_hours", 24) or 24)
    if not _mail(doc, "setup_email_template", SETUP_TEMPLATE, ctx):
        return False
    frappe.db.set_value("Tenant", doc.name, "setup_email_sent_at", now_datetime())
    return True


def send_tenant_setup(tenant):
    """Email the tenant owner "Complete your setup": one-time "set your password" link (minted here, in the job).
    Atomic no-op if already sent: the check, send and mark all happen under a lock."""
    name = tenant if isinstance(tenant, str) else tenant.name
    with audit.cache_lock(f"saas_core:setup_mail:{name}", ttl=60, wait=10):
        doc = frappe.get_doc("Tenant", name)
        if doc.get("setup_email_sent_at"):
            return False
        return _send_setup(doc)


def send_tenant_welcome(tenant):
    """Welcome email (site URL and next steps; no link key). Called only after the site reported setup complete.
    Atomic no-op if already sent."""
    name = tenant if isinstance(tenant, str) else tenant.name
    with audit.cache_lock(f"saas_core:welcome:{name}", ttl=60, wait=10):
        doc = frappe.get_doc("Tenant", name)
        if doc.get("welcome_sent_at"):
            return False
        if not doc.get("setup_completed_at"):
            return False   # never welcome a tenant that has not finished setup
        if not _mail(doc, "welcome_email_template", DEFAULT_TEMPLATE, _context(doc, _site(doc))):
            return False
        frappe.db.set_value("Tenant", doc.name, "welcome_sent_at", now_datetime())
        return True


def resend_setup_link(tenant):
    """Explicit admin action: mint a NEW setup link and re-send the "Complete your setup" mail, bypassing the
    setup_email_sent_at guard on purpose (owner lost/expired the first link). Caller must be authorised
    (e.g. an admin.tenants endpoint behind require_admin)."""
    name = tenant if isinstance(tenant, str) else tenant.name
    with audit.cache_lock(f"saas_core:setup_mail:{name}", ttl=60, wait=10):
        ok = _send_setup(frappe.get_doc("Tenant", name))
    if ok:
        audit.log("tenant.setup_link_resent", target_doctype="Tenant", target=name)
    return ok
