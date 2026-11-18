"""Day-based billing: billing.preview_cancel (GET), billing.cancel (POST), billing.preview_change (GET)."""
from __future__ import annotations

import frappe

from ._common import (ApiError, endpoint, gateway_for, idempotency_key, idempotent, json_body, latest_subscription,
                      not_found, rate_limit, request_hash, require_fields, require_login, tenant_access)

_CONFLICTS = {"already_canceling": "This subscription is already canceled or canceling",
              "plan_change_pending": "A plan change is scheduled. Try again after midnight IST",
              "plan_change_pending_payment": "An upgrade payment is pending. Pay or wait for it to expire, then cancel"}


def _sub(tenant, ptype="read", user=None):
    require_fields({"tenant": tenant}, "tenant")
    tenant_access(tenant, user, ptype)
    sub = latest_subscription(tenant=tenant)
    if not sub:
        raise not_found("Subscription")
    return sub


@endpoint()
def preview_cancel(tenant: str | None = None):
    """What cancelling now would cost and refund. Read-only."""
    require_login()
    from saas_core.billing import cancellation
    return cancellation.quote(_sub(tenant))


@endpoint()
def cancel(tenant: str | None = None, reason: str | None = None):
    """Day-based cancel: site stays active to the end of today (IST); unused days are refunded."""
    body = json_body(locals())
    user = require_login()
    rate_limit(f"billing.cancel:{user}", 10, 3600)
    sub = _sub(body.get("tenant"), "write", user)
    from saas_core.billing import cancellation

    def run():
        try:
            return cancellation.cancel(sub, user, reason=body.get("reason"))
        except cancellation.CancelError as e:
            if e.code in _CONFLICTS:
                raise ApiError(e.code, _CONFLICTS[e.code], 409)
            raise ApiError("gateway_error", "Payment provider unavailable, try again", 502)

    return idempotent(idempotency_key(), request_hash({**body}), run, scope="billing.cancel")


@endpoint()
def preview_change(tenant: str | None = None, plan: str | None = None):
    """Prorated quote for moving to `plan`. Read-only."""
    require_login()
    require_fields({"tenant": tenant, "plan": plan}, "tenant", "plan")
    sub = _sub(tenant)
    if not frappe.db.exists("Plan", plan):
        raise not_found("Plan")
    from saas_core.billing import plan_change
    return plan_change.preview(sub, plan)
