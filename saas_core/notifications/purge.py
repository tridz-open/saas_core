"""Scrub one-time setup links from sent Email Queue rows so they do not linger in the database."""
from __future__ import annotations

import re

import frappe

_KEY = re.compile(r"(/update-password\?key=)[^\s\"'<>&]+")
REDACTED = "/update-password?key=[redacted]"


def redact(text: str | None) -> str | None:
    return _KEY.sub(REDACTED, text) if text else text


def purge_sent_setup_links() -> int:
    """Redact setup keys in Tenant-linked Email Queue rows already Sent over an hour ago. Returns rows changed."""
    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
    rows = frappe.get_all("Email Queue", filters={"reference_doctype": "Tenant", "status": "Sent",
                                                  "modified": ["<", cutoff]},
                          fields=["name", "message"], limit_page_length=0)
    changed = 0
    for r in rows:
        new = redact(r.get("message"))
        if new != r.get("message"):
            frappe.db.set_value("Email Queue", r["name"], "message", new, update_modified=False)
            changed += 1
    if changed:
        frappe.db.commit()
    return changed
