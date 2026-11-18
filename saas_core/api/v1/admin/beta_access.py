"""admin.beta_access.list / add / remove / resend. System Manager only (not SaaS Admin); everyone else gets 403."""
from __future__ import annotations

import frappe

from saas_core import beta_access as core
from saas_core.api.v1._common import ApiError, endpoint, json_body, rate_limit, require_fields, require_login


def _require_system_manager() -> str:
    user = require_login()
    if user != "Administrator" and "System Manager" not in set(frappe.get_roles(user)):
        raise ApiError("forbidden", "System Manager role required", 403)
    return user


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@endpoint()
def list():  # noqa: A001
    """Patterns plus, per exact email, whether the account exists / is verified / owns a tenant."""
    admin = _require_system_manager()
    rate_limit(f"admin.beta_access.list:{admin}", 120, 60)
    return core.list_entries()


@endpoint()
def add(entries=None, send_invite=False, note: str | None = None):
    admin = _require_system_manager()
    rate_limit(f"admin.beta_access.add:{admin}", 30, 60)
    body = json_body(locals())
    require_fields(body, "entries")
    try:
        return core.add_entries(body["entries"], send_invite=_truthy(body.get("send_invite")),
                                note=body.get("note"), actor=admin)
    except core.BetaAccessError as e:
        raise ApiError("validation_error", str(e), 422, e.details)


@endpoint()
def remove(pattern: str | None = None):
    admin = _require_system_manager()
    rate_limit(f"admin.beta_access.remove:{admin}", 60, 60)
    body = json_body(locals())
    require_fields(body, "pattern")
    return core.remove_entry(body["pattern"], actor=admin)


@endpoint()
def resend(email: str | None = None):
    admin = _require_system_manager()
    rate_limit(f"admin.beta_access.resend:{admin}", 30, 60)
    body = json_body(locals())
    require_fields(body, "email")
    try:
        return core.resend_invite(body["email"], actor=admin)
    except core.BetaAccessError as e:
        raise ApiError("validation_error", str(e), 422, e.details)
