"""templates.list: public site templates."""
from __future__ import annotations

import frappe

from ._common import endpoint, paginate
from .serializers import to_template


def _schema(slug: str) -> dict:
    try:
        from saas_core.recipes import registry
        recipe = registry.get(slug)
        return dict(recipe.signup_schema()) if recipe else {}
    except Exception:
        return {}


@endpoint(allow_guest=True)
def list(cursor: str | None = None, limit: int | None = None):  # noqa: A001
    def ser(row):
        doc = frappe.get_doc("Template", row["name"])
        return to_template(doc, _schema(doc.get("recipe") or doc.get("slug") or doc.get("name")))
    return paginate("Template", {"is_public": 1}, ["name"], cursor, limit, order_by="name asc", serializer=ser)
