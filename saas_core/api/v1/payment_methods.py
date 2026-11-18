"""payment_methods.portal_link: gateway billing portal for the caller's subscription."""
from __future__ import annotations

from ._common import (endpoint, gateway_for, json_body, latest_subscription, not_found, rate_limit, require_login,
                      tenant_access, utc_in)


@endpoint()
def portal_link(tenant: str | None = None, return_url: str | None = None):
    body = json_body(locals())
    user = require_login()
    rate_limit(f"portal:{user}", 30, 3600)
    if body.get("tenant"):
        tenant_access(body["tenant"], user)
        sub = latest_subscription(tenant=body["tenant"])
    else:
        sub = latest_subscription(user=user)
    if not sub:
        raise not_found("Subscription")
    url = gateway_for(sub.get("gateway")).portal_link(sub.get("gateway_customer_id"), body.get("return_url"))
    return {"url": url, "expires_at": utc_in(3600)}
