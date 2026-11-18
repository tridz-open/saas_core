"""subscription.get. Cancellation is billing.cancel (day-based pro-rata refund policy); portal link is payment_methods.portal_link."""
from __future__ import annotations

from ._common import endpoint, latest_subscription, require_fields, require_login, tenant_access, not_found
from .serializers import to_subscription


def _sub(tenant: str):
    require_fields({"tenant": tenant}, "tenant")
    tenant_access(tenant)
    sub = latest_subscription(tenant=tenant)
    if not sub:
        raise not_found("Subscription")
    return sub


@endpoint()
def get(tenant: str | None = None):
    require_login()
    return to_subscription(_sub(tenant))
