"""admin.ops.run: one-shot platform ops for hosts with no bench shell (Frappe Cloud). System Manager only."""
from __future__ import annotations

import importlib
import re

import frappe

from saas_core.api.v1._common import ApiError, endpoint, json_body, require_fields

# op -> (module, function). Resolved lazily so importing this module never touches gateway code.
OPS = {
    "bootstrap_gateway": ("saas_core.gateways.bootstrap", "from_site_config"),
    "setup_email": ("saas_core.scripts.setup_email_account", "setup"),
    "sync_plans": ("saas_core.gateways.razorpay_sync", "sync_plans"),
    "fc_check": ("saas_core.provisioning.fc_client", "fc_check"),
}
OP_ROLES = {"fc_check": "SaaS Admin"}  # every other op: System Manager
_SECRET_HINTS = ("pass", "pwd", "secret", "token", "key")
_SECRET_VALUE = re.compile(
    r"(rzp_(live|test)_|rzp_|sk_(live|test)_|sk_|pk_live_|whsec_|rk_(live|test)_)[A-Za-z0-9_\-]*"
    r"|\b[0-9a-fA-F]{32,}\b|\b[A-Za-z0-9+/_\-]{40,}={0,2}")


def _scrub(v):
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items() if not any(h in str(k).lower() for h in _SECRET_HINTS)}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    if isinstance(v, str):
        return _SECRET_VALUE.sub("[redacted]", v)
    return v if isinstance(v, (int, float, bool, type(None))) else _scrub(str(v))


@endpoint(methods=["POST"])
def run(op: str | None = None):
    body = json_body(locals())
    require_fields(body, "op")
    op = body["op"]
    frappe.only_for(OP_ROLES.get(op, "System Manager"))
    if op not in OPS:
        raise ApiError("validation_error", "Unknown op", 422, {"op": f"one of {sorted(OPS)}"})
    mod, fn = OPS[op]
    result = getattr(importlib.import_module(mod), fn)()
    from saas_core import audit
    audit.log("admin.ops.run", actor=frappe.session.user, details={"op": op})
    return {"op": op, "result": _scrub(result)}
