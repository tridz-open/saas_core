"""Session ops: signup, login, logout, me."""
from __future__ import annotations

import re

import frappe

from ._common import (ApiError, client_ip, current_user, endpoint, idempotency_key, idempotent, json_body,
                      rate_limit, request_hash, require_fields, require_login, set_status)
from .serializers import to_me

CUSTOMER_ROLE = "SaaS Customer"
MIN_PASSWORD = 10
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_password_policy(pw: str) -> str | None:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)):
        return "Password must contain letters and digits"
    return None


def _csrf() -> str | None:
    try:
        sessions = getattr(frappe, "sessions", None)
        if sessions and hasattr(sessions, "get_csrf_token"):
            return sessions.get_csrf_token()
        return frappe.local.session.data.csrf_token
    except Exception:
        return None


def public_signup_enabled() -> bool:
    """site_config `saas_core_public_signup` (then Platform Settings): unset => enabled; 0/false/off disables."""
    from saas_core.config import get_setting
    v = get_setting("public_signup", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


_GATE_MESSAGES = {
    "signup_disabled": "Signup is currently closed",
    "not_allowlisted": "This email address is not on the signup allowlist",
    "invite_required": "Signup requires a valid invite",
}


def _legacy_restricts() -> bool:
    """Deprecated `saas_core_public_signup`: may only further restrict (explicit falsy closes); truthy/unset never opens."""
    return not public_signup_enabled()


def _valid_invite(token, email: str | None = None) -> str | None:
    """Return the invited Tenant Member name if token matches that member's email, is unexpired; else None."""
    if not token or not isinstance(token, str) or not email:
        return None
    from saas_core.api.v1.tenants.team import hash_token
    rows = frappe.get_all("Tenant Member", filters={"invite_token_hash": hash_token(token), "status": "invited"},
                          fields=["name", "email", "expires_at"])
    if not rows:
        return None
    row = rows[0]
    if str(row.get("email") or "").strip().lower() != email.strip().lower():
        return None
    exp = row.get("expires_at")
    if not exp or frappe.utils.get_datetime(exp) < frappe.utils.now_datetime():
        return None
    return row.get("name")


def _enforce_signup_gate(email: str, invite_token):
    """Apply saas_core_signup_mode (legacy flag can only restrict). Returns the matched invite member or None."""
    from saas_core.config.signup import is_signup_allowed
    if _legacy_restricts():
        raise ApiError("signup_disabled", _GATE_MESSAGES["signup_disabled"], 403)
    member = _valid_invite(invite_token, email)
    ok, reason = is_signup_allowed(email, invite_ok=bool(member))
    if not ok:
        reason = reason or "signup_disabled"
        raise ApiError(reason, _GATE_MESSAGES.get(reason, "Signup not allowed"), 403)
    return member


def _email_verified(user: str) -> bool:
    """Same rule the server enforces at checkout (config.signup.is_email_verified): only allowlist mode needs a verified email."""
    from saas_core.config.signup import is_email_verified
    return is_email_verified(user)


def _me(user: str, with_csrf: bool = False) -> dict:
    info = frappe.db.get_value("User", user, ["email", "full_name"], as_dict=True)
    info = info or {}
    out = to_me(user, info.get("email"), info.get("full_name"), frappe.get_roles(user),
                frappe.db.count("Tenant", {"owner_user": user}), _csrf() if with_csrf else None,
                email_verified=_email_verified(user))
    out["public_signup"] = public_signup_enabled()
    return out


def _login_manager():
    from frappe.auth import LoginManager
    return LoginManager()


@endpoint(allow_guest=True)
def config():
    """Public (guest) UI config: {public_signup: bool, signup_mode: str, payment_banner: str | None, site_domain: str | None}."""
    banner = None
    try:
        from saas_core.gateways.manual import payment_banner
        banner = payment_banner()
    except Exception:  # noqa: BLE001
        pass
    from saas_core.config.signup import signup_mode
    mode = signup_mode()
    gate_open = mode in ("open", "allowlist", "invite")
    from saas_core.config import get_setting
    domain = (get_setting("site_domain", None) or "").strip().lstrip(".") or None
    return {"public_signup": bool(gate_open and public_signup_enabled()), "signup_mode": mode,
            "payment_banner": banner, "site_domain": domain}


@endpoint(allow_guest=True)
def signup(email: str | None = None, password: str | None = None, full_name: str | None = None,
           plan: str | None = None, template: str | None = None, recipe_fields: dict | None = None,
           invite_token: str | None = None):
    rate_limit(f"signup:{client_ip()}", 10, 3600)
    if not public_signup_enabled():
        raise ApiError("signup_disabled", "Public signup is disabled", 403)
    body = json_body(locals())
    require_fields(body, "email", "password")
    email = str(body["email"]).strip().lower()
    invite_member = _enforce_signup_gate(email, body.get("invite_token"))
    errs = {}
    if not _EMAIL.match(email):
        errs["email"] = "Invalid email address"
    msg = check_password_policy(body["password"])
    if msg:
        errs["password"] = msg
    if body.get("plan") and not frappe.db.exists("Plan", body["plan"]):
        errs["plan"] = "Unknown plan"
    if body.get("template") and not frappe.db.exists("Template", body["template"]):
        errs["template"] = "Unknown template"
    if errs:
        raise ApiError("validation_error", "Invalid signup details", 422, errs)

    def create():
        if frappe.db.exists("User", email):
            # P2-7: generic; no field detail. Fully neutral 202 is not feasible: success logs in (201 + session/CSRF
            # for the SDK), so any non-201 would still distinguish; rate_limit above throttles probing.
            raise ApiError("conflict", "We could not create an account with these details", 409)
        name = (body.get("full_name") or email.split("@")[0]).strip()
        doc = frappe.get_doc({
            "doctype": "User", "name": email, "email": email, "first_name": name, "full_name": name,
            "enabled": 1, "send_welcome_email": 0, "email_verified": 1 if invite_member else 0, "new_password": body["password"], "user_type": "Website User",
            "roles": [{"role": CUSTOMER_ROLE}],
        })
        doc.insert(ignore_permissions=True)
        if invite_member:  # token bound to this email proves ownership; consume it (single use)
            frappe.db.set_value("Tenant Member", invite_member,
                                {"status": "active", "user": email, "invite_token_hash": ""})
        try:
            lm = _login_manager()
            lm.authenticate(user=email, pwd=body["password"])
            lm.post_login()
        except ImportError:
            frappe.session.user = email
        frappe.db.commit()
        set_status(201)
        return _me(email, with_csrf=True)

    return idempotent(idempotency_key(), request_hash({k: v for k, v in body.items() if k != "password"}),
                      create, scope="signup")


@endpoint(allow_guest=True)
def login(email: str | None = None, password: str | None = None):
    body = json_body(locals())
    require_fields(body, "email", "password")
    email = str(body["email"]).strip().lower()
    rate_limit(f"login-ip:{client_ip()}", 30, 300)
    rate_limit(f"login:{client_ip()}:{email}", 10, 300)
    try:
        lm = _login_manager()
        lm.authenticate(user=email, pwd=body["password"])
        lm.post_login()
    except frappe.AuthenticationError:
        raise ApiError("unauthorized", "Invalid email or password", 401)
    return _me(current_user() if current_user() != "Guest" else email, with_csrf=True)


@endpoint()
def logout():
    require_login()
    _login_manager().logout()
    set_status(204)
    return None


@endpoint()
def me():
    user = require_login()
    rate_limit(f"me:{user}", 300, 60)
    return _me(user, with_csrf=True)
