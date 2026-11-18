"""admin.tenants.create / invite / list. SaaS Admin (or System Manager) only; everyone else gets 403."""
from __future__ import annotations

import re

import frappe

from saas_core.config import get_setting

from saas_core.api.v1._common import (ApiError, endpoint, json_body, paginate, rate_limit, require_fields,
                                      require_login, set_status, utc_in)
from saas_core.api.v1.serializers import to_tenant

ADMIN_ROLES = frozenset({"SaaS Admin", "System Manager"})
CUSTOMER_ROLE = "SaaS Customer"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def require_admin() -> str:
    user = require_login()
    if user != "Administrator" and not (ADMIN_ROLES & set(frappe.get_roles(user))):
        raise ApiError("forbidden", "SaaS Admin role required", 403)
    return user


def _send_invite(email: str, title: str | None = None) -> bool:
    """Owner invite: Frappe password-reset-link style mail. Best effort; returns whether a mail was queued."""
    title = title or "your restaurant"
    try:
        user = frappe.get_doc("User", email)
        if get_setting("admin_invite_email_template", ""):
            gen = getattr(user, "_reset_password", None) or user.reset_password
            link = gen(send_email=False)
            from saas_core.notifications.templating import brand_name, console_url, send
            send("admin_invite", [email], f"Set up your {brand_name()} account",
                 f"We created a {brand_name()} account for you. Set your password to start (one-time link): {link}",
                 {"email": email, "setup_url": link, "tenant_title": title, "setup_link_hours": 24,
                  "login_url": console_url("login")})  # SPA route under the console base
        else:
            user.reset_password(send_email=True)
        return True
    except Exception:
        pass
    try:
        from saas_core.notifications.templating import brand_name, console_url, send
        send("admin_invite", [email], f"Set up your {brand_name()} account",
             f"We created a {brand_name()} account for you. Open {console_url('forgot-password')} to set your password.",
             {"email": email, "tenant_title": title, "login_url": console_url("login")})
        return True
    except Exception:
        return False


def _ensure_owner(email: str, full_name: str | None) -> bool:
    if frappe.db.exists("User", email):
        return False
    name = (full_name or email.split("@")[0]).strip()
    frappe.get_doc({"doctype": "User", "name": email, "email": email, "first_name": name, "full_name": name,
                    "enabled": 1, "send_welcome_email": 0, "user_type": "Website User",
                    "roles": [{"role": CUSTOMER_ROLE}]}).insert(ignore_permissions=True)
    return True


def _ensure_subscription(owner: str, tenant: str, plan: str) -> None:
    if frappe.db.exists("Subscription", {"owner_user": owner, "status": ["in", ["active", "trialing"]]}):
        return
    frappe.get_doc({"doctype": "Subscription", "tenant": tenant, "owner_user": owner, "plan": plan,
                    "gateway": "manual", "status": "active", "interval": "month",
                    "period_end": utc_in(30 * 24 * 3600)}).insert(ignore_permissions=True)


@endpoint()
def create(owner_email: str | None = None, slug: str | None = None, plan: str | None = None,
           owner_name: str | None = None, template: str | None = None, region: str | None = None,
           recipe_fields: dict | None = None):
    admin = require_admin()
    rate_limit(f"admin.tenants.create:{admin}", 30, 60)
    body = json_body(locals())
    require_fields(body, "owner_email", "slug", "plan")
    email = str(body["owner_email"]).strip().lower()
    if not _EMAIL.match(email):
        raise ApiError("validation_error", "Invalid email address", 422, {"owner_email": "Invalid email address"})
    from saas_core.api.v1.tenants import _check_slug, _new_tenant
    _check_slug(body["slug"])
    if not frappe.db.exists("Plan", body["plan"]):
        raise ApiError("validation_error", "Unknown plan", 422, {"plan": "unknown plan"})
    tpl = body.get("template") or frappe.db.get_value("Plan", body["plan"], "template")
    if tpl and not frappe.db.exists("Template", tpl):
        raise ApiError("validation_error", "Unknown template", 422, {"template": "unknown template"})
    rf = body.get("recipe_fields")
    if isinstance(rf, str):
        rf = frappe.parse_json(rf)
    created = _ensure_owner(email, body.get("owner_name"))
    invited = _send_invite(email) if created else False
    tenant = _new_tenant(email, body["slug"], body["plan"], tpl, body.get("region"), rf, created_via="admin")
    _ensure_subscription(email, tenant.name, body["plan"])
    from saas_core import jobs, audit
    job_name = jobs.enqueue_job(tenant.name, "provision", dedupe_key=f"provision:{tenant.name}")
    audit.log("admin.tenant.create", actor=admin, target_doctype="Tenant", target=tenant.name,
              details={"owner": email, "plan": body["plan"]})
    frappe.db.commit()
    set_status(202)
    return {"tenant": to_tenant(tenant), "job": job_name, "owner_created": created, "invite_sent": invited}


@endpoint()
def invite(tenant: str | None = None):
    admin = require_admin()
    rate_limit(f"admin.tenants.invite:{admin}", 30, 60)
    body = json_body(locals())
    require_fields(body, "tenant")
    if not frappe.db.exists("Tenant", body["tenant"]):
        raise ApiError("not_found", "Tenant not found", 404)
    owner = frappe.db.get_value("Tenant", body["tenant"], "owner_user")
    from saas_core.notifications.templating import display_title
    sent = _send_invite(owner, display_title(body["tenant"]))
    from saas_core import audit
    audit.log("admin.tenant.invite", actor=admin, target_doctype="Tenant", target=body["tenant"], details={"owner": owner})
    return {"tenant": body["tenant"], "owner_email": owner, "invite_sent": sent}


@endpoint(methods=["POST"])
def resend_setup_link(tenant: str | None = None):
    admin = require_admin()
    rate_limit(f"admin.tenants.resend_setup_link:{admin}", 30, 60)
    body = json_body(locals())
    require_fields(body, "tenant")
    if not frappe.db.exists("Tenant", body["tenant"]):
        raise ApiError("not_found", "Tenant not found", 404)
    from saas_core.notifications import welcome
    sent = bool(welcome.resend_setup_link(body["tenant"]))
    from saas_core import audit
    audit.log("admin.tenant.resend_setup_link", actor=admin, target_doctype="Tenant", target=body["tenant"],
              details={"sent": sent})
    return {"tenant": body["tenant"], "sent": sent}


@endpoint()
def list(cursor: str | None = None, limit: int | None = None, status: str | None = None,
         owner_email: str | None = None):  # noqa: A001
    admin = require_admin()
    rate_limit(f"admin.tenants.list:{admin}", 120, 60)
    filters = {}
    if status:
        filters["status"] = status
    if owner_email:
        filters["owner_user"] = owner_email.strip().lower()
    page = paginate("Tenant", filters, ["name"], cursor, limit)
    items = []
    for r in page["items"]:
        d = frappe.get_doc("Tenant", r["name"])
        items.append({**to_tenant(d), "owner_email": d.get("owner_user")})
    return {"items": items, "next_cursor": page["next_cursor"]}
