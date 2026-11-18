"""Support ticket ops: support.tickets.list/get/create/reply (customers only see their own tickets)."""
from __future__ import annotations

import frappe

from saas_core.api.v1._common import authenticated, ApiError, paginate, rate_limit, require_login, require_experimental
from saas_core.api.v1.serializers import to_ticket
from saas_core.api.v1.tenants import access, not_found, run_idempotent

PRIORITIES = ("low", "normal", "high")


def _own(ticket, user):
    if not ticket or not isinstance(ticket, str):
        raise ApiError("validation_error", "ticket is required", 400, {"ticket": "required"})
    try:
        doc = frappe.get_doc("Support Ticket", ticket)
    except frappe.DoesNotExistError:
        raise not_found("Ticket")
    if doc.owner_user != user:
        raise not_found("Ticket")
    return doc


@authenticated()
def list(cursor=None, limit=None):
    require_experimental("support")
    user = require_login()
    rate_limit(f"tickets.list:{user}", 120, 60)
    page = paginate("Support Ticket", {"owner_user": user}, ["name"], cursor, limit, order_by="creation desc")
    return {"items": [to_ticket(frappe.get_doc("Support Ticket", r["name"])) for r in page["items"]],
            "next_cursor": page["next_cursor"]}


@authenticated()
def get(ticket=None):
    require_experimental("support")
    user = require_login()
    rate_limit(f"tickets.get:{user}", 240, 60)
    return to_ticket(_own(ticket, user))


@authenticated(methods=["POST"])
def create(subject=None, body=None, tenant=None, priority=None):
    require_experimental("support")
    user = require_login()
    rate_limit(f"tickets.create:{user}", 10, 60)
    errs = {}
    if not subject or not str(subject).strip():
        errs["subject"] = "required"
    elif len(subject) > 200:
        errs["subject"] = "max 200 characters"
    if not body or not str(body).strip():
        errs["body"] = "required"
    if priority and priority not in PRIORITIES:
        errs["priority"] = "must be low, normal or high"
    if errs:
        raise ApiError("validation_error", "Invalid ticket", 400, errs)
    if tenant:
        access(tenant, user)

    def do():
        doc = frappe.get_doc({"doctype": "Support Ticket", "tenant": tenant or None, "owner_user": user,
                              "subject": subject.strip(), "body": body, "status": "open",
                              "priority": priority or "normal"})
        doc.insert(ignore_permissions=True)
        return to_ticket(doc)

    return run_idempotent("tickets.create", user, {"s": subject, "b": body, "t": tenant, "p": priority}, do, 201)


@authenticated(methods=["POST"])
def reply(ticket=None, body=None):
    require_experimental("support")
    user = require_login()
    rate_limit(f"tickets.reply:{user}", 30, 60)
    doc = _own(ticket, user)
    if not body or not str(body).strip():
        raise ApiError("validation_error", "body is required", 400, {"body": "required"})
    if doc.status == "closed":
        raise ApiError("validation_error", "Ticket is closed", 400)
    doc.append("replies", {"from_staff": 0, "body": body, "at": frappe.utils.now_datetime(), "author": user})
    doc.status = "open"
    doc.save(ignore_permissions=True)
    return to_ticket(doc)


@authenticated(methods=["POST"])
def close(ticket=None):
    require_experimental("support")
    user = require_login()
    rate_limit(f"tickets.close:{user}", 30, 60)
    doc = _own(ticket, user)
    if doc.status != "closed":
        doc.status = "closed"
        doc.save(ignore_permissions=True)
    return to_ticket(doc)
