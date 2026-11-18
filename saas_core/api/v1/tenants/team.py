"""Tenant team ops: tenants.team.list/invite/revoke_invite/change_role/remove (owner/admin only)."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

import frappe

from saas_core.api.v1._common import authenticated, ApiError, idempotent, paginate, rate_limit, require_login, require_experimental

INVITE_TTL = timedelta(days=7)
INVITABLE = ("admin", "member", "viewer")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PRIV = frozenset({"SaaS Admin", "System Manager"})


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _nf(what="Resource"):
    return ApiError("not_found", f"{what} not found", 404)


def _status(code):
    if frappe.local.get("response") is None:
        frappe.local.response = frappe._dict()
    frappe.local.response["http_status_code"] = code


def _full_name(email):
    try:
        return frappe.db.get_value("User", email, "full_name")
    except Exception:
        return None


def to_member(doc) -> dict:
    status = doc.get("status")
    return {
        "name": doc.get("name"), "email": doc.get("email"), "full_name": _full_name(doc.get("user") or doc.get("email")),
        "role": doc.get("role"), "status": status,
        "invited_at": _iso(doc.get("creation")) if status == "invited" else None,
    }


def _owner_row(tenant) -> dict:
    email = tenant.owner_user
    return {"name": f"owner:{tenant.name}", "email": email, "full_name": _full_name(email), "role": "owner",
            "status": "active", "invited_at": None}


def manage_access(tenant_name, user):
    """Tenant doc if `user` is tenant owner, platform-privileged, or an active tenant admin.
    Unknown tenant / not a member => 404 (no enumeration); lower-privileged member => 403."""
    if not tenant_name or not isinstance(tenant_name, str):
        raise ApiError("validation_error", "tenant is required", 400, {"tenant": "required"})
    if not frappe.db.exists("Tenant", tenant_name):
        raise _nf("Tenant")
    t = frappe.get_doc("Tenant", tenant_name)
    if user == "Administrator" or t.get("owner_user") == user or (_PRIV & set(frappe.get_roles(user))):
        return t
    rows = frappe.get_all("Tenant Member", filters={"tenant": tenant_name, "user": user, "status": "active"},
                          fields=["name", "role"])
    if not rows:
        raise _nf("Tenant")
    if rows[0].get("role") != "admin":
        raise ApiError("forbidden", "Only owners and admins can manage the team", 403)
    return t


def _member(tenant, member, *, invited=None):
    if not member or not isinstance(member, str):
        raise ApiError("validation_error", "member is required", 400, {"member": "required"})
    if member == f"owner:{tenant.name}":
        raise ApiError("conflict", "The owner cannot be changed or removed", 409)
    if not frappe.db.exists("Tenant Member", member):
        raise _nf("Member")
    doc = frappe.get_doc("Tenant Member", member)
    if doc.get("tenant") != tenant.name or (invited is not None and (doc.get("status") == "invited") != invited):
        raise _nf("Member")
    if doc.get("role") == "owner" and doc.get("status") == "active":
        raise ApiError("conflict", "The owner cannot be changed or removed", 409)
    return doc


def _role(role, allow_owner=False):
    if role == "owner":
        raise ApiError("validation_error", "Ownership cannot be assigned", 422, {"role": "owner not assignable"})
    if role not in INVITABLE:
        raise ApiError("validation_error", "Invalid role", 422, {"role": "must be admin, member or viewer"})
    return role


def _idem(scope, user, payload, fn):
    from . import _safe_idem_header
    key = _safe_idem_header() or frappe.form_dict.get("idempotency_key")
    if not key:
        return fn()
    h = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()
    return idempotent(key, h, fn, scope)


@authenticated()
def list(tenant=None, cursor=None, limit=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.list:{user}", 120, 60)
    t = manage_access(tenant, user)
    page = paginate("Tenant Member", {"tenant": t.name}, ["name"], cursor, limit, order_by="creation desc")
    items = [to_member(frappe.get_doc("Tenant Member", r["name"])) for r in page["items"]]
    if not cursor:
        items.insert(0, _owner_row(t))
    return {"items": items, "next_cursor": page["next_cursor"]}


@authenticated(methods=["POST"])
def invite(tenant=None, email=None, role=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.invite:{user}", 20, 60)
    t = manage_access(tenant, user)

    def do():
        em = str(email or "").strip().lower()
        if not _EMAIL.match(em):
            raise ApiError("validation_error", "Invalid email address", 422, {"email": "invalid"})
        r = _role(role)
        if em == str(t.owner_user or "").lower() or frappe.get_all(
                "Tenant Member", filters={"tenant": t.name, "email": em}, fields=["name"]):
            raise ApiError("conflict", "That person is already on the team or invited", 409, {"email": "exists"})
        token = secrets.token_urlsafe(32)
        doc = frappe.get_doc({"doctype": "Tenant Member", "tenant": t.name, "email": em, "role": r,
                              "status": "invited", "invite_token_hash": hash_token(token), "invited_by": user,
                              "expires_at": frappe.utils.now_datetime() + INVITE_TTL})
        doc.insert(ignore_permissions=True)
        _send_invite(em, t, token, user, r)
        from saas_core import audit
        audit.log("team.invite", actor=user, target_doctype="Tenant Member", target=doc.name,
                  details={"tenant": t.name, "email": em, "role": r})
        _status(201)
        return to_member(doc)

    return _idem("team.invite", user, {"t": tenant, "e": email, "r": role}, do)


def _send_invite(email, tenant, token, inviter=None, role=None):
    sender = getattr(frappe, "sendmail", None)
    if not sender:
        return
    try:
        from saas_core.notifications.templating import brand_name, console_url, display_title, send
        url = console_url(f"accept-invite?token={token}")   # SPA route under the console base, absolute
        title = display_title(tenant)
        who = ""
        if inviter:
            who = frappe.db.get_value("User", inviter, "full_name") or ""
        who = who or "A teammate"
        days = INVITE_TTL.days
        send("team_invite", [email], f"{who} invited you to {title} on {brand_name()}",
             f"{who} invited you to join {title} on {brand_name()} as {role or 'a team member'}. "
             f"Accept the invite (expires in {days} days): {url}",
             {"invite_url": url, "tenant_title": title, "email": email, "inviter_name": who,
              "role": role or "", "expires_days": days}, now=False)
    except Exception:
        frappe.log_error("team invite email failed")


@authenticated(methods=["POST"])
def revoke_invite(tenant=None, invite=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.revoke_invite:{user}", 30, 60)
    t = manage_access(tenant, user)
    doc = _member(t, invite, invited=True)
    doc.delete(ignore_permissions=True)
    from saas_core import audit
    audit.log("team.revoke_invite", actor=user, target_doctype="Tenant Member", target=invite,
              details={"tenant": t.name, "email": doc.get("email")})
    _status(204)


@authenticated(methods=["POST"])
def change_role(tenant=None, member=None, role=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.change_role:{user}", 30, 60)
    t = manage_access(tenant, user)
    doc = _member(t, member)
    r = _role(role)
    old = doc.get("role")
    doc.role = r
    doc.save(ignore_permissions=True)
    from saas_core import audit
    audit.log("team.change_role", actor=user, target_doctype="Tenant Member", target=member,
              details={"tenant": t.name, "from": old, "to": r})
    return to_member(doc)


@authenticated(methods=["POST"])
def remove(tenant=None, member=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.remove:{user}", 30, 60)
    t = manage_access(tenant, user)
    doc = _member(t, member)
    doc.delete(ignore_permissions=True)
    from saas_core import audit
    audit.log("team.remove", actor=user, target_doctype="Tenant Member", target=member,
              details={"tenant": t.name, "email": doc.get("email"), "role": doc.get("role")})
    _status(204)
