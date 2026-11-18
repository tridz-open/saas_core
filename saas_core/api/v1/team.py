"""Team invite acceptance: team.accept_invite (token-based, any logged-in user whose email matches)."""
from __future__ import annotations

import frappe

from saas_core.api.v1._common import authenticated, ApiError, rate_limit, require_login, require_experimental


@authenticated(methods=["POST"])
def accept_invite(token=None):
    require_experimental("team")
    user = require_login()
    rate_limit(f"team.accept_invite:{user}", 10, 300)
    if not token or not isinstance(token, str):
        raise ApiError("validation_error", "token is required", 422, {"token": "required"})
    from saas_core.api.v1.tenants.team import hash_token, to_member
    rows = frappe.get_all("Tenant Member", filters={"invite_token_hash": hash_token(token), "status": "invited"},
                          fields=["name"])
    if not rows:
        raise ApiError("not_found", "Invite not found", 404)
    doc = frappe.get_doc("Tenant Member", rows[0]["name"])
    me = str(user).strip().lower()
    if str(doc.get("email") or "").lower() != me:
        raise ApiError("not_found", "Invite not found", 404)  # token is for someone else; don't confirm it exists
    exp = doc.get("expires_at")
    if exp and frappe.utils.get_datetime(exp) < frappe.utils.now_datetime():
        raise ApiError("validation_error", "This invite has expired", 422, {"token": "expired"})
    if frappe.get_all("Tenant Member", filters={"tenant": doc.get("tenant"), "user": user, "status": "active"},
                      fields=["name"]):
        raise ApiError("conflict", "You are already on this team", 409)
    doc.update({"status": "active", "user": user, "invite_token_hash": None, "expires_at": None})
    doc.save(ignore_permissions=True)
    from saas_core import audit
    audit.log("team.accept_invite", actor=user, target_doctype="Tenant Member", target=doc.name,
              details={"tenant": doc.get("tenant"), "role": doc.get("role")})
    return to_member(doc)
