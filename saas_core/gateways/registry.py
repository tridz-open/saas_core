"""Gateway factory: builds a Gateway from a Gateway Account doc (secrets via get_password)."""
from __future__ import annotations

import json

import frappe

from .base import Gateway
from .manual import ManualGateway
from .razorpay import RazorpayGateway
from .stripe import StripeGateway

GATEWAYS = {"razorpay": RazorpayGateway, "stripe": StripeGateway,
            "manual": ManualGateway}


def get_gateway(name: str, account: str | None = None, **overrides) -> Gateway:
    """Load the enabled Gateway Account (by name, else first enabled for `name`) and build the gateway."""
    cls = GATEWAYS.get(name)
    if not cls:
        frappe.throw(f"Unknown gateway: {name}")
    if name == "stripe":
        from saas_core.experimental import enabled
        if not enabled("stripe"):
            frappe.throw("The Stripe gateway is experimental (no refunds, not live-tested); enable it with "
                         "site_config saas_core_experimental=stripe")
    if not account:
        filters = {"gateway": name, "enabled": 1}
        rows = frappe.get_all("Gateway Account", filters=filters, pluck="name", limit=1)
        if not rows:
            frappe.throw(f"No enabled Gateway Account for {name}")
        account = rows[0]
    doc = frappe.get_doc("Gateway Account", account)
    if doc.get("gateway") != name:
        frappe.throw(f"Gateway Account {account} is not a {name} account")
    if name == "manual" and doc.get("mode") == "live":
        frappe.throw("The manual (mock payment) gateway cannot be used in live mode")
    if name == "manual":
        from .policy import live_gateway_enabled
        if live_gateway_enabled():
            frappe.throw("The manual (mock payment) gateway is disabled while a live gateway account is enabled")
    extra = doc.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra or "{}")
    return cls(key_id=doc.get("key_id") or "",
               key_secret=doc.get_password("key_secret", raise_exception=False) or "",
               webhook_secret=doc.get_password("webhook_secret", raise_exception=False) or "",
               extra=extra, **overrides)
