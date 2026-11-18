"""invoices.list / get / pdf."""
from __future__ import annotations

import frappe

from ._common import (ApiError, _response, current_user, endpoint, not_found, paginate, require_fields,
                      require_login, tenant_access)
from .serializers import to_invoice

_FIELDS = ["name"]


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
    resp = _response()
    if str(url).startswith(("http://", "https://")):
        resp["type"] = "redirect"
        resp["location"] = url
        return None
    fname = frappe.db.get_value("File", {"file_url": url}, "name")
    if not fname:
        raise ApiError("not_available", "Invoice PDF file is missing", 404)
    resp["filename"] = f"{doc.get('number') or doc.get('name')}.pdf"
    resp["filecontent"] = frappe.get_doc("File", fname).get_content()
    resp["type"] = "download"
    return None
