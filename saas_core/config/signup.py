"""Signup gate config: mode (closed/invite/allowlist/open) + allowlist match."""
import frappe

from saas_core.config import get_setting

MODES = ("closed", "invite", "allowlist", "open")


def signup_mode() -> str:
    m = str(get_setting("signup_mode", "closed") or "closed").strip().lower()
    return m if m in MODES else "closed"


def _patterns() -> list[str]:
    try:
        rows = frappe.get_doc("Platform Settings").get("signup_allow") or []
    except Exception:
        return []
    return [str(r.get("pattern") or "").strip().lower() for r in rows if r.get("pattern")]


def is_signup_allowed(email: str, invite_ok: bool = False) -> tuple[bool, str | None]:
    """Return (allowed, reason). Reasons: signup_disabled, invite_required, not_allowlisted."""
    mode = signup_mode()
    if mode == "open":
        return True, None
    if mode == "closed":
        return False, "signup_disabled"
    if mode == "invite":
        return (True, None) if invite_ok else (False, "invite_required")
    em = (email or "").strip().lower()
    for p in _patterns():
        if p.startswith("@"):
            if em.endswith(p) and len(em) > len(p):
                return True, None
        elif em == p:
            return True, None
    return False, "not_allowlisted"


def is_email_verified(user: str) -> bool:
    """False only in allowlist mode for an account whose email is unverified. Call from checkout/tenant creation."""
    if signup_mode() != "allowlist":
        return True
    return bool(frappe.db.get_value("User", user, "email_verified"))
