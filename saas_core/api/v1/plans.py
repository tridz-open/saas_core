"""plans.list: public plan catalogue."""
from __future__ import annotations

import frappe

from ._common import endpoint
from .serializers import to_plan


@endpoint(allow_guest=True)
def list(currency: str | None = None, interval: str | None = None):  # noqa: A001
    if interval and interval not in ("month", "year"):
        from ._common import ApiError
        raise ApiError("bad_request", "Invalid interval", 400, {"interval": "must be month or year"})
    capacity = True
    try:
        from saas_core.billing.entitlements import capacity_open
        capacity = bool(capacity_open())
    except ImportError:
        pass
    names = frappe.get_all("Plan", filters={"is_public": 1}, fields=["name"], order_by="sort_order asc")
    items = []
    for r in names:
        plan = to_plan(frappe.get_doc("Plan", r["name"]))
        plan["prices"] = [p for p in plan["prices"]
                          if (not currency or p["currency"] == currency.upper() or p["currency"] == currency)
                          and (not interval or p["interval"] == interval)]
        if (currency or interval) and not plan["prices"]:
            continue
        plan["available"] = plan["available"] and capacity
        items.append(plan)
    banner = None
    try:
        from saas_core.gateways.manual import payment_banner
        banner = payment_banner()
    except Exception:
        pass
    return {"items": items, "next_cursor": None, "payment_banner": banner}
