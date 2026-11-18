"""Email verification: request (current user) and confirm (key from the emailed link)."""
from __future__ import annotations

import frappe

from ._common import ApiError, client_ip, endpoint, json_body, rate_limit, require_fields, require_login, set_status

TTL = 24 * 3600
_CK = "saas_core:verify_email:"


def _send(email: str, key: str) -> None:
    sender = getattr(frappe, "sendmail", None)
    if not sender:
        return
    try:
        from saas_core.notifications.templating import brand_name, console_url, send
        url = console_url(f"verify-email?key={key}")   # SPA route under the console base, absolute
        send("verify_email", [email], f"Confirm your email for {brand_name()}",
             f"Confirm {email} to finish creating your {brand_name()} account (valid {TTL // 3600} hours): {url}",
             {"verify_url": url, "email": email, "expires_hours": TTL // 3600}, now=False)
    except Exception:
        frappe.log_error("saas_core verify email send failed")


@endpoint()
def request():
    user = require_login()
    rate_limit(f"verify-email:{user}", 3, 3600)
    key = frappe.generate_hash(length=32)
    frappe.cache().set_value(_CK + key, user, expires_in_sec=TTL)
    _send(frappe.db.get_value("User", user, "email") or user, key)
    set_status(202)
    return None


@endpoint()
def confirm(key: str | None = None):
    user = require_login()
    body = json_body(locals())
    require_fields(body, "key")
    rate_limit(f"verify-email-confirm:{client_ip()}", 10, 3600)
    ck = _CK + str(body["key"])
    owner = frappe.cache().get_value(ck)
    if owner != user:
        raise ApiError("invalid_token", "Verification link is invalid or has expired", 400)
    frappe.db.set_value("User", user, "email_verified", 1)
    frappe.cache().delete_value(ck)
    set_status(204)
    return None
