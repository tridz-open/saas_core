"""Provision job read ops: jobs.get / jobs.list (customer-safe; ownership via the job's tenant)."""
from __future__ import annotations

import frappe

from saas_core.api.v1._common import authenticated, ApiError, paginate, rate_limit, require_login
from saas_core.api.v1.tenants import access, not_found, safe_job


@authenticated()
def get(job=None):
    user = require_login()
    rate_limit(f"jobs.get:{user}", 300, 60)
    if not job or not isinstance(job, str):
        raise ApiError("validation_error", "job is required", 400, {"job": "required"})
    try:
        doc = frappe.get_doc("Provision Job", job)
    except frappe.DoesNotExistError:
        raise not_found("Job")
    access(doc.tenant, user)  # non-owner => 404
    return safe_job(doc)


@authenticated()
def list(cursor=None, limit=None, tenant=None):
    user = require_login()
    rate_limit(f"jobs.list:{user}", 120, 60)
    t = access(tenant, user)
    page = paginate("Provision Job", {"tenant": t.name}, ["name"], cursor, limit, order_by="creation desc")
    return {"items": [safe_job(frappe.get_doc("Provision Job", r["name"])) for r in page["items"]],
            "next_cursor": page["next_cursor"]}
