"""Configurable email templates: get_setting("<key>_email_template") names an Email Template; falls back to plain text."""
import frappe

from saas_core.config import get_setting


def absolute_url(path):
    """Prefix a relative ("/...") path with the control-plane origin; leave absolute/empty values alone."""
    if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
        return frappe.utils.get_url().rstrip("/") + path
    return path


def console_url(path: str = "") -> str:
    """Absolute customer-console URL (`<console_url>/<path>`), the base all SPA links in emails must use."""
    base = (get_setting("console_url", "/console") or "/console").rstrip("/")
    return absolute_url(base + ("/" + path.lstrip("/") if path else ""))


def fmt_date(value, utc=False) -> str:
    """'17 Nov 2026' from a date/datetime/ISO string; '' when empty or unparseable (templates hide empty rows).
    `utc=True`: a naive datetime is naive UTC (billing fields) and is shown in the site's system timezone."""
    import datetime as _dt
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value.strip().replace("Z", "")[:19])
        except ValueError:
            return ""
    if utc and isinstance(value, _dt.datetime):
        try:
            value = frappe.utils.convert_utc_to_system_timezone(value)
        except Exception:  # noqa: BLE001 - tz lookup failure: show the UTC date
            pass
    try:
        return f"{value.day} {value.strftime('%b %Y')}"
    except AttributeError:
        return ""


def display_title(tenant_doc_or_name=None) -> str:
    """Customer-facing restaurant name: the title, else 'your restaurant'. Never the slug or the tenant id."""
    t = tenant_doc_or_name
    if isinstance(t, str):
        try:
            t = frappe.get_doc("Tenant", t) if frappe.db.exists("Tenant", t) else None
        except Exception:  # noqa: BLE001
            t = None
    title = (t.get("title") if t else "") or ""
    if not str(title).strip() and t:
        # Tenant has no title field: the name the customer typed lives in the signup recipe fields.
        rf = t.get("recipe_fields") or {}
        if isinstance(rf, str):
            try:
                rf = frappe.parse_json(rf) or {}
            except Exception:  # noqa: BLE001
                rf = {}
        title = (rf.get("restaurant_name") or rf.get("company") or "") if isinstance(rf, dict) else ""
    return str(title).strip() or "your restaurant"


def plan_label(plan) -> str:
    """'Standard' from a plan code like 'acme-standard' (Plan.title when available).
    A shared code prefix can be stripped with setting `plan_code_prefix` (e.g. "acme-")."""
    if not plan:
        return ""
    try:
        title = frappe.db.get_value("Plan", plan, "title")
        if title:
            return str(title)
    except Exception:  # noqa: BLE001
        pass
    code = str(plan)
    prefix = str(get_setting("plan_code_prefix", "") or "").lower()
    if prefix and code.lower().startswith(prefix):
        code = code[len(prefix):]
    return code.replace("-", " ").replace("_", " ").strip().title()


def host_display(url) -> str:
    """Site URL without scheme or trailing slash, for link text."""
    return str(url or "").split("://", 1)[-1].rstrip("/")


def absolutize(ctx: dict) -> dict:
    """Make logo_url and every other *_url value that starts with "/" absolute."""
    return {k: absolute_url(v) if k.endswith("_url") else v for k, v in ctx.items()}


def brand_name() -> str:
    return str(get_setting("brand", "Your Platform"))


def brand_context(**extra) -> dict:
    ctx = {"brand": get_setting("brand", "Your Platform"), "brand_color": get_setting("brand_color", "#4f46e5"),
           "logo_url": get_setting("logo_url", ""), "logo_url_dark": get_setting("logo_url_dark", ""), "support_email": get_setting("support_email", "support@example.com")}
    ctx.update(extra)
    return absolutize(ctx)


# Shipped branded templates (fixtures/email_template.json), used unless `<key>_email_template` overrides them.
DEFAULT_TEMPLATES = {"beta_invite": "saas-beta-invite", "invoice": "saas-invoice-paid", "payment_failed": "saas-payment-failed", "dunning": "saas-dunning",
                     "cancellation": "saas-cancellation", "refund_processed": "saas-refund-processed", "renewal_receipt": "saas-renewal-receipt"}


def render(key: str, ctx: dict):
    """Return (subject, html) from the Email Template named by setting `<key>_email_template` (or the shipped
    default for `key`), else None."""
    name = get_setting(f"{key}_email_template", "") or DEFAULT_TEMPLATES.get(key, "")
    if not name or not frappe.db.exists("Email Template", name):
        return None
    tpl = frappe.get_doc("Email Template", name)
    return frappe.render_template(tpl.subject, ctx), frappe.render_template(tpl.response_html or tpl.response, ctx)


def send(key: str, recipients, subject: str, message: str, ctx: dict, **kw):
    r = render(key, brand_context(**ctx))
    if r:
        subject, message = r
    return frappe.sendmail(recipients=recipients, subject=subject, message=message, **kw)
