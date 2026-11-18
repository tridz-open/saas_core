"""Profile ops: `me` (re-exported from auth) and `update`."""
from __future__ import annotations

import sys
import types

import frappe

from ._common import ApiError, endpoint, json_body, rate_limit, require_login
from .auth import _me, check_password_policy, me  # noqa: F401  (me re-exported)


@endpoint()
def update(full_name: str | None = None, current_password: str | None = None, new_password: str | None = None):
    user = require_login()
    body = json_body(locals())
    rate_limit(f"me_update:{user}", 20, 300)
    errs: dict = {}
    has_cur, has_new = bool(body.get("current_password")), bool(body.get("new_password"))
    if has_cur != has_new:
        errs["current_password" if has_new else "new_password"] = "current_password and new_password must be sent together"
    name = body.get("full_name")
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > 140:
            errs["full_name"] = "Invalid name"
    if has_new and has_cur:
        msg = check_password_policy(body["new_password"])
        if msg:
            errs["new_password"] = msg
    if errs:
        raise ApiError("validation_error", "Invalid profile update", 422, errs)
    if name is None and not has_new:
        raise ApiError("bad_request", "Nothing to update", 400)
    if has_new:
        from frappe.utils.password import check_password, update_password
        try:
            check_password(user, body["current_password"])
        except frappe.AuthenticationError:
            raise ApiError("validation_error", "Current password is incorrect", 422,
                           {"current_password": "incorrect"})
        update_password(user, body["new_password"])
    if name is not None:
        frappe.db.set_value("User", user, {"first_name": name, "full_name": name}, update_modified=False)
    frappe.db.commit()
    return _me(user)


# `saas_core.api.v1.me` must stay callable even after this submodule is imported (the import system rebinds the
# package attribute `me` to this module), so make the module itself a whitelisted delegate to auth.me.
class _CallableModule(types.ModuleType):
    def __call__(self, *a, **k):
        return me(*a, **k)


sys.modules[__name__].__class__ = _CallableModule
try:
    _wl = getattr(frappe, "whitelisted", None)
    if isinstance(_wl, list):
        _wl.append(sys.modules[__name__])
except Exception:  # pragma: no cover
    pass
