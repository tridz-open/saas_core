"""Authenticate `Authorization: Bearer <secret>` against API Token rows (registered via hooks.auth_hooks).

`<secret>` is the value `api_tokens.create` returns once; only its sha256 is stored. Frappe's own
`token key:secret` and Basic auth are untouched (scheme must be exactly `Bearer`), and a Bearer token that
Frappe's OAuth layer already accepted (session user is not Guest) is left alone. Any other Bearer value that
does not match an active token is rejected with 401 in the API Error shape - never silently downgraded to Guest.
The secret is never logged or echoed."""
from __future__ import annotations

import datetime as dt
import hmac
import json

import frappe

DT = "API Token"
LAST_USED_GRANULARITY = dt.timedelta(minutes=1)
_MESSAGE = "Invalid or expired API token"


def _reject():
    from werkzeug.exceptions import Unauthorized
    from werkzeug.wrappers import Response
    from saas_core.api.v1._common import ApiError, error_response
    body = error_response(ApiError("unauthorized", _MESSAGE, 401))
    raise Unauthorized(response=Response(json.dumps(body), 401, mimetype="application/json",
                                         headers={"WWW-Authenticate": 'Bearer realm="api"'}))


def _bearer_secret() -> str | None:
    header = (frappe.get_request_header("Authorization") or "").strip()
    if not header:
        return None
    parts = header.split(None, 1)
    if parts[0].lower() != "bearer":
        return None  # Basic / `token key:secret` / anything else: not ours
    return parts[1].strip() if len(parts) == 2 else ""


def _feature_enabled() -> bool:
    from saas_core.experimental import ExperimentalDisabled, require
    try:
        require("api_tokens")
    except ExperimentalDisabled:
        return False
    return True


def _find_active(secret: str):
    from saas_core.api.v1.api_tokens import hash_secret
    digest = hash_secret(secret)
    rows = frappe.get_all(DT, filters={"token_hash": digest}, limit=5,
                          fields=["name", "owner_user", "tenant", "token_hash", "expires_at", "revoked", "last_used"])
    now = dt.datetime.utcnow()
    for r in rows:
        if not hmac.compare_digest(str(r.get("token_hash") or ""), digest):
            continue
        if r.get("revoked"):
            continue
        exp = r.get("expires_at")
        if exp:
            exp = dt.datetime.fromisoformat(str(exp)) if not isinstance(exp, dt.datetime) else exp
            if exp <= now:
                continue
        if frappe.db.get_value("User", r.get("owner_user"), "enabled") == 0:
            continue
        return r
    return None


def validate() -> None:
    """auth_hooks entry point. No-op unless the request carries a Bearer credential."""
    secret = _bearer_secret()
    if secret is None:
        return
    if getattr(frappe.session, "user", None) not in (None, "", "Guest"):
        return  # already authenticated (e.g. Frappe OAuth bearer)
    row = _find_active(secret) if secret and _feature_enabled() else None
    if not row:
        _reject()
    frappe.set_user(row["owner_user"])
    frappe.local.api_token = row["name"]
    frappe.local.api_token_tenant = row.get("tenant") or None
    now = dt.datetime.utcnow()
    last = row.get("last_used")
    if last and not isinstance(last, dt.datetime):
        last = dt.datetime.fromisoformat(str(last))
    if not last or now - last >= LAST_USED_GRANULARITY:
        frappe.db.set_value(DT, row["name"], "last_used", now, update_modified=False)
        frappe.db.commit()
