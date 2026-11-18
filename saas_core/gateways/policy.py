"""Which gateway a checkout may use. The client never picks: only System Manager can override the default."""
from __future__ import annotations

import frappe

from saas_core.config import get_setting


def default_gateway() -> str:
    """Effective default: the platform setting, else the first enabled Gateway Account's gateway, else razorpay."""
    name = get_setting("default_gateway")
    if name:
        return str(name)
    try:
        rows = frappe.get_all("Gateway Account", filters={"enabled": 1}, fields=["gateway"],
                              order_by="creation asc", limit=1)
    except Exception:
        rows = []
    return (rows[0].get("gateway") if rows else None) or "razorpay"


def live_gateway_enabled() -> bool:
    """True when any enabled non-manual Gateway Account is in live mode."""
    try:
        rows = frappe.get_all("Gateway Account", filters={"enabled": 1}, fields=["gateway", "mode"])
    except Exception:
        return False
    return any(r.get("gateway") not in ("manual", "fake") and r.get("mode") == "live" for r in rows)


def is_system_manager(user: str | None = None) -> bool:
    try:
        return "System Manager" in (frappe.get_roles(user) or [])
    except Exception:
        return False


def resolve_gateway(client_gateway: str | None, user: str | None = None) -> str:
    """The client's `gateway` is honoured only when it equals the effective default, or for System Manager."""
    eff = default_gateway()
    if client_gateway and client_gateway != eff and is_system_manager(user):
        return client_gateway
    return eff
