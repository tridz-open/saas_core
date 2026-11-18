"""Personal API tokens: list / create / revoke. Only sha256(secret) is stored; secret shown once."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

import frappe

from ._common import (ApiError, endpoint, require_experimental, idempotency_key, idempotent, json_body, not_found, paginate,
                      rate_limit, request_hash, require_fields, require_login, set_status, tenant_access)
from .serializers import iso

DT = "API Token"
_FIELDS = ["name", "label", "tenant", "prefix", "created", "last_used", "expires_at", "revoked", "owner_user"]


def _g(r, k):
    return r.get(k) if hasattr(r, "get") else getattr(r, k, None)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _ser(r) -> dict:
    return {"name": _g(r, "name"), "label": _g(r, "label"), "tenant": _g(r, "tenant") or None,
            "prefix": _g(r, "prefix") or "", "created_at": iso(_g(r, "created")),
            "last_used_at": iso(_g(r, "last_used")), "expires_at": iso(_g(r, "expires_at"))}


@endpoint()
def list(cursor: str | None = None, limit: int | None = None):
    require_experimental("api_tokens")
    user = require_login()
    rate_limit(f"api_tokens:{user}", 120, 60)
    body = json_body(locals())
    return paginate(DT, {"owner_user": user, "revoked": 0}, _FIELDS, body.get("cursor"), body.get("limit"),
                    order_by="created desc", serializer=_ser)


def _parse_expiry(v):
    if v in (None, ""):
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("validation_error", "Invalid expires_at", 422, {"expires_at": "invalid"})
    if d.tzinfo:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if d <= dt.datetime.utcnow():
        raise ApiError("validation_error", "expires_at must be in the future", 422, {"expires_at": "must be future"})
    return d


@endpoint()
def create(label: str | None = None, tenant: str | None = None, expires_at: str | None = None):
    require_experimental("api_tokens")
    user = require_login()
    body = json_body(locals())
    require_fields(body, "label")
    rate_limit(f"api_tokens_create:{user}", 20, 3600)
    label = str(body["label"]).strip()
    errs = {}
    if not label or len(label) > 100:
        errs["label"] = "label must be 1..100 characters"
    if errs:
        raise ApiError("validation_error", "Invalid token details", 422, errs)
    expiry = _parse_expiry(body.get("expires_at"))
    tenant = body.get("tenant") or None
    if tenant:
        tenant_access(tenant, user)  # non-owner => 404

    def make():
        api_key = "sk_" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        doc = frappe.get_doc({"doctype": DT, "owner_user": user, "label": label, "tenant": tenant,
                              "prefix": api_key[:8], "token_hash": hash_secret(secret),
                              "created": dt.datetime.utcnow(), "expires_at": expiry, "revoked": 0})
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        set_status(201)
        return {"token": _ser(doc), "secret": secret, "api_key": api_key}

    # a replay returns the originally issued secret (cached 24h) - same Idempotency-Key + same payload only
    return idempotent(idempotency_key(), request_hash({"l": label, "t": tenant, "e": str(expiry)}), make,
                      scope="api_tokens")


@endpoint()
def revoke(token: str | None = None):
    user = require_login()
    body = json_body(locals())
    require_fields(body, "token")
    rate_limit(f"api_tokens_revoke:{user}", 60, 60)
    rows = frappe.get_all(DT, filters={"name": body["token"], "owner_user": user}, fields=["name", "revoked"])
    if not rows:
        raise not_found("Token")
    if not rows[0].get("revoked"):
        frappe.db.set_value(DT, rows[0]["name"], "revoked", 1)
        frappe.db.commit()
    set_status(204)
    return None
