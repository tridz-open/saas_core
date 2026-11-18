"""Beta access: manage the signup allowlist (Platform Settings `signup_allow`) and email invites.

Shared by the admin API (`saas_core.api.v1.admin.beta_access`) and the bench CLI wrappers
(`add_beta_users`, `remove_beta_users`, `list_beta_users`, `resend_beta_invite`). No permission checks here:
the API layer enforces System Manager, and `bench execute` runs as Administrator.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import frappe

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_DOMAIN = re.compile(r"^@[^@\s,;]+\.[^@\s,;]+$")
MAX_ENTRIES = 200


class BetaAccessError(ValueError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def split_entries(entries) -> list[str]:
    """Accept a list, or a string separated by newlines/commas/semicolons/whitespace. Lowercased, deduped, ordered."""
    if entries is None:
        return []
    if isinstance(entries, str):
        entries = re.split(r"[\s,;]+", entries)
    out, seen = [], set()
    for e in entries:
        p = str(e or "").strip().lower()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def classify(pattern: str) -> str | None:
    if _EMAIL.match(pattern):
        return "email"
    if _DOMAIN.match(pattern):
        return "domain"
    return None


def _doc():
    return frappe.get_doc("Platform Settings")


def _rows(doc) -> list:
    return list(doc.get("signup_allow") or [])


def _audit(action: str, actor: str | None, details: dict) -> None:
    try:
        from saas_core import audit
        audit.log(action, actor=actor, target_doctype="Platform Settings", target="Platform Settings", details=details)
    except Exception:  # noqa: BLE001 - audit is best effort here
        pass


def _row_status(row) -> dict:
    pattern = str(row.get("pattern") or "").strip().lower()
    out = {"pattern": pattern, "kind": classify(pattern) or "email", "note": row.get("note") or None,
           "invited_at": str(row.get("invited_at")) if row.get("invited_at") else None,
           "account_exists": None, "verified": None, "has_tenant": None}
    if out["kind"] == "email":
        exists = bool(frappe.db.exists("User", pattern))
        out["account_exists"] = exists
        out["verified"] = bool(exists and frappe.db.get_value("User", pattern, "email_verified"))
        out["has_tenant"] = bool(exists and frappe.db.exists("Tenant", {"owner_user": pattern}))
    return out


def list_entries() -> dict:
    from saas_core.config.signup import signup_mode
    return {"signup_mode": signup_mode(), "patterns": [_row_status(r) for r in _rows(_doc())]}


def invite_link(email: str) -> str:
    from saas_core.notifications.templating import console_url
    return console_url("signup?email=" + quote(email, safe="@"))


def _mark_invited(doc, email: str) -> None:
    for r in _rows(doc):
        if str(r.get("pattern") or "").strip().lower() == email:
            r.invited_at = frappe.utils.now_datetime()


def send_invite_email(email: str) -> bool:
    """Branded "You're invited to try <brand>" mail with a signup link. Best effort; True when queued."""
    try:
        from saas_core.notifications.templating import send
        url = invite_link(email)
        from saas_core.notifications.templating import brand_name
        b = brand_name()
        send("beta_invite", [email], f"You're invited to try {b}",
             f"You're invited to try {b}. Create your account here: {url}",
             {"email": email, "signup_url": url}, now=False)
        return True
    except Exception:  # noqa: BLE001
        frappe.log_error("saas_core beta invite send failed", "saas_core beta access")
        return False


def add_entries(entries, send_invite: bool = False, note: str | None = None, actor: str | None = None) -> dict:
    items = split_entries(entries)
    if not items:
        raise BetaAccessError("No entries given", {"entries": "required"})
    if len(items) > MAX_ENTRIES:
        raise BetaAccessError(f"At most {MAX_ENTRIES} entries per request", {"entries": "too many"})
    invalid = [p for p in items if not classify(p)]
    if invalid:
        raise BetaAccessError("Invalid entries: " + ", ".join(invalid[:5]), {"invalid": invalid})
    note = (str(note).strip() if note else "") or None
    doc = _doc()
    existing = {str(r.get("pattern") or "").strip().lower() for r in _rows(doc)}
    added, already = [], []
    for p in items:
        if p in existing:
            already.append(p)
            continue
        row = {"pattern": p}
        if note:
            row["note"] = note
        doc.append("signup_allow", row)
        existing.add(p)
        added.append(p)
    sent, failed = [], []
    if send_invite:
        for p in items:
            if classify(p) != "email":
                continue
            if send_invite_email(p):
                sent.append(p)
                _mark_invited(doc, p)
            else:
                failed.append(p)
    if added or sent:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        _audit("beta_access.add", actor, {"added": added, "invited": sent})
    return {"added": added, "already_listed": already, "invites_sent": sent, "invites_failed": failed,
            "skipped_domains": [p for p in items if send_invite and classify(p) == "domain"]}


def remove_entry(pattern: str, actor: str | None = None) -> dict:
    p = str(pattern or "").strip().lower()
    doc = _doc()
    rows = _rows(doc)
    keep = [r for r in rows if str(r.get("pattern") or "").strip().lower() != p]
    if len(keep) == len(rows):
        raise frappe.DoesNotExistError(p)
    if callable(getattr(doc, "set", None)):
        doc.set("signup_allow", keep)
    else:
        doc.signup_allow = keep
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    _audit("beta_access.remove", actor, {"pattern": p})
    return {"removed": p}


def resend_invite(email: str, actor: str | None = None) -> dict:
    e = str(email or "").strip().lower()
    if classify(e) != "email":
        raise BetaAccessError("Only an exact email can be invited", {"email": "invalid"})
    doc = _doc()
    if e not in {str(r.get("pattern") or "").strip().lower() for r in _rows(doc)}:
        raise frappe.DoesNotExistError(e)
    ok = send_invite_email(e)
    if ok:
        _mark_invited(doc, e)
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        _audit("beta_access.resend", actor, {"email": e})
    return {"email": e, "sent": ok}


# ---- bench CLI wrappers: bench --site X execute saas_core.beta_access.<fn> --kwargs '{...}' ----------------------
def add_beta_users(emails=None, send_invite: bool = False, note: str | None = None) -> dict:
    """Whitelist emails and/or @domains; optionally email invites (exact emails only)."""
    return add_entries(emails, send_invite=bool(send_invite), note=note, actor="Administrator")


def remove_beta_users(emails=None) -> dict:
    gone, missing = [], []
    for p in split_entries(emails):
        try:
            remove_entry(p, actor="Administrator")
            gone.append(p)
        except frappe.DoesNotExistError:
            missing.append(p)
    return {"removed": gone, "not_found": missing}


def list_beta_users() -> dict:
    return list_entries()


def resend_beta_invite(email: str) -> dict:
    return resend_invite(email, actor="Administrator")
