"""invoices.list / get / pdf."""
from __future__ import annotations

from urllib.parse import urlsplit

import frappe

from ._common import (ApiError, _response, current_user, endpoint, not_found, paginate, require_fields,
                      require_login, tenant_access)
from .serializers import to_invoice

_FIELDS = ["name"]

# Gateway-hosted invoice / payment-page hosts that `pdf_url` may legitimately redirect to (exact match, https only).
# Extend per site with site_config `saas_core_pdf_url_hosts` (list or comma-separated string).
DEFAULT_PDF_HOSTS = (
    "rzp.io",                  # Razorpay short_url (invoices, subscriptions, payment links)
    "razorpay.com",
    "pages.razorpay.com",
    "invoices.razorpay.com",
    "checkout.stripe.com",     # Stripe (experimental gateway)
    "invoice.stripe.com",
    "pay.stripe.com",
)
_FILE_PREFIXES = ("/files/", "/private/files/")


def _allowed_hosts() -> set[str]:
    extra = frappe.conf.get("saas_core_pdf_url_hosts") or []
    if isinstance(extra, str):
        extra = extra.split(",")
    return {h.strip().lower() for h in (*DEFAULT_PDF_HOSTS, *extra) if h and h.strip()}


def _is_allowed_redirect(url: str) -> bool:
    """True only for https URLs with no userinfo/port tricks whose exact host is allowlisted."""
    if not url or any(c in url for c in "\\\t\r\n ") or url != url.strip():
        return False
    try:
        u = urlsplit(url)
        host = u.hostname
        u.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    if u.scheme != "https" or not host or u.username is not None or u.password is not None:
        return False
    return host.lower().rstrip(".") in _allowed_hosts()


def _is_local_file_path(url: str) -> bool:
    return url.startswith(_FILE_PREFIXES) and ".." not in url and "\\" not in url


def _load(invoice: str, user: str):
    require_fields({"invoice": invoice}, "invoice")
    if not frappe.db.exists("Invoice", invoice):
        raise not_found("Invoice")
    doc = frappe.get_doc("Invoice", invoice)
    if doc.get("owner_user") != user:
        tenant_access(doc.get("tenant"), user)  # 404 when not allowed
    return doc


@endpoint()
def list(tenant: str | None = None, cursor: str | None = None, limit: int | None = None):  # noqa: A001
    user = require_login()
    if tenant:
        tenant_access(tenant, user)
        filters = {"tenant": tenant}
    else:
        filters = {"owner_user": user}
    return paginate("Invoice", filters, ["name"], cursor, limit, order_by="creation desc",
                    serializer=lambda r: to_invoice(frappe.get_doc("Invoice", r["name"])))


@endpoint()
def get(invoice: str | None = None):
    return to_invoice(_load(invoice, require_login()))


@endpoint()
def pdf(invoice: str | None = None):
    doc = _load(invoice, require_login())
    url = doc.get("pdf_url")
    if not url:
        raise ApiError("not_available", "No PDF is generated for this invoice; saas_core only links the "
                       "gateway-hosted payment page for open invoices", 404)
    url = str(url)
    resp = _response()
    if _is_allowed_redirect(url):
        resp["type"] = "redirect"
        resp["location"] = url
        return None
    if not _is_local_file_path(url):
        raise ApiError("not_available", "Invoice PDF is not available", 404)
    fname = frappe.db.get_value("File", {"file_url": url}, "name")
    if not fname:
        raise ApiError("not_available", "Invoice PDF file is missing", 404)
    resp["filename"] = f"{doc.get('number') or doc.get('name')}.pdf"
    resp["filecontent"] = frappe.get_doc("File", fname).get_content()
    resp["type"] = "download"
    return None
