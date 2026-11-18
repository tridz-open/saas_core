"""Password reset: request (always 202, no enumeration) and confirm."""
from __future__ import annotations

import frappe

from saas_core.config import get_setting

from ._common import ApiError, client_ip, endpoint, json_body, rate_limit, require_fields, set_status
from .auth import check_password_policy


@endpoint(allow_guest=True)
def request(email: str | None = None):
    body = json_body(locals())
    require_fields(body, "email")
    email = str(body["email"]).strip().lower()
    rate_limit(f"pwreset:{client_ip()}", 10, 3600)
    rate_limit(f"pwreset-user:{email}", 3, 3600)
    try:
        if frappe.db.exists("User", email) and frappe.db.get_value("User", email, "enabled"):
            user = frappe.get_doc("User", email)
            if get_setting("password_reset_email_template", ""):
                # Branded path: generate the link without Frappe's mail, send via the configured template.
                gen = getattr(user, "_reset_password", None) or user.reset_password
                link = gen(send_email=False)
                from saas_core.notifications.templating import send
                send("password_reset", [email], "Reset your password",
                     f"We received a request to reset the password for {email}. Choose a new password: {link}",
                     {"email": email, "reset_url": link})
            else:
                user.reset_password(send_email=True)
    except Exception:
        frappe.log_error("saas_core password reset request failed")
    set_status(202)
    return None


@endpoint(allow_guest=True)
def confirm(key: str | None = None, new_password: str | None = None):
    body = json_body(locals())
    require_fields(body, "key", "new_password")
    rate_limit(f"pwconfirm:{client_ip()}", 10, 3600)
    msg = check_password_policy(body["new_password"])
    if msg:
        raise ApiError("validation_error", msg, 422, {"new_password": msg})
    from frappe.core.doctype.user.user import update_password
    try:
        update_password(body["new_password"], key=body["key"])
    except Exception:
        raise ApiError("invalid_token", "Reset link is invalid or has expired", 400)
    set_status(204)
    return None
