"""Gateway factory: resolves a gateway class (built-in or plugin) and builds it from a Gateway Account doc.

Plugins: any installed Frappe app registers gateways in its hooks.py, no saas_core edit needed:

    saas_core_gateways = {"mygw": "my_app.gateways.MyGateway"}

The class must subclass `saas_core.gateways.base.Gateway`, set `name` equal to the key, and declare `supports`
(a frozenset of keys of base.CAPABILITIES). A plugin cannot replace a built-in name. See docs/gateways/.
"""
from __future__ import annotations

import json

import frappe

from .base import CAPABILITIES, Gateway
from .fake import FakeGateway
from .manual import ManualGateway
from .razorpay import RazorpayGateway
from .stripe import StripeGateway

HOOK = "saas_core_gateways"

# Built-ins. Tests may monkeypatch entries (e.g. GATEWAYS["razorpay"]).
GATEWAYS = {"razorpay": RazorpayGateway, "stripe": StripeGateway, "manual": ManualGateway, "fake": FakeGateway}

# gateway name -> experimental feature (saas_core.experimental.FEATURES) that must be enabled to use it
EXPERIMENTAL = {"stripe": "stripe", "fake": "fake_gateway"}

_EXPERIMENTAL_HINT = {
    "stripe": "The Stripe gateway is experimental (no refunds, not live-tested); enable it with "
              "site_config saas_core_experimental=stripe",
    "fake": "The fake gateway is for tests and dev only; enable it with site_config saas_core_experimental=fake_gateway",
}


def _hook_entries() -> dict[str, object]:
    """name -> dotted path (or class) from every installed app's `saas_core_gateways` hook."""
    try:
        raw = frappe.get_hooks(HOOK) or {}
    except Exception:  # hooks unavailable (very early boot): built-ins only
        return {}
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: dict[str, object] = {}
    for item in items:
        if isinstance(item, dict):
            out.update(item)
        elif isinstance(item, (list, tuple)):  # frappe may hand back a list of dicts
            for d in item:
                if isinstance(d, dict):
                    out.update(d)
    return out


def _load_plugin(name: str, target):
    if name in GATEWAYS:
        frappe.throw(f"Gateway plugin '{name}' cannot replace the built-in gateway of the same name")
    cls = target
    if isinstance(target, str):
        try:
            cls = frappe.get_attr(target)
        except Exception:
            frappe.throw(f"Gateway plugin '{name}': cannot import '{target}'")
    validate_gateway_class(name, cls)
    return cls


def validate_gateway_class(name: str, cls) -> None:
    """Throw (ValidationError) unless `cls` is a usable Gateway: subclass of Gateway, `name` == registry key,
    `supports` a frozenset of known capabilities. Plugin authors: run the conformance kit for the full check."""
    if not (isinstance(cls, type) and issubclass(cls, Gateway)):
        frappe.throw(f"Gateway plugin '{name}': {cls!r} must subclass saas_core.gateways.base.Gateway")
    if not getattr(cls, "name", ""):
        frappe.throw(f"Gateway plugin '{name}': class {cls.__name__} must set a non-empty `name`")
    if cls.name != name:
        frappe.throw(f"Gateway plugin '{name}': class {cls.__name__}.name is '{cls.name}', it must equal the hook key")
    sup = getattr(cls, "supports", None)
    if not isinstance(sup, frozenset):
        frappe.throw(f"Gateway plugin '{name}': class {cls.__name__} must declare `supports` as a frozenset")
    unknown = sorted(set(sup) - set(CAPABILITIES))
    if unknown:
        frappe.throw(f"Gateway plugin '{name}': unknown capabilities {unknown}; allowed: {sorted(CAPABILITIES)}")
    if getattr(cls, "__abstractmethods__", None):
        frappe.throw(f"Gateway plugin '{name}': {cls.__name__} does not implement "
                     f"{sorted(cls.__abstractmethods__)}")


def gateway_class(name: str):
    """Class for gateway `name`: a built-in, else a plugin from hooks. Throws for unknown or invalid ones."""
    if name in GATEWAYS:
        return GATEWAYS[name]
    entries = _hook_entries()
    if name in entries:
        return _load_plugin(name, entries[name])
    frappe.throw(f"Unknown gateway: {name}")


def available_gateways() -> list[str]:
    """Sorted names of built-ins plus plugin names declared in hooks (not validated, not experimental-filtered)."""
    return sorted(set(GATEWAYS) | set(_hook_entries()))


def get_gateway(name: str, account: str | None = None, **overrides) -> Gateway:
    """Load the enabled Gateway Account (by name, else first enabled for `name`) and build the gateway."""
    cls = gateway_class(name)
    feature = EXPERIMENTAL.get(name) or getattr(cls, "experimental_feature", None)
    if feature:
        from saas_core.experimental import enabled
        if not enabled(feature):
            frappe.throw(_EXPERIMENTAL_HINT.get(name) or f"The {name} gateway is experimental; enable it with "
                         f"site_config saas_core_experimental={feature}")
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
    if name == "fake" and doc.get("mode") == "live":
        frappe.throw("The fake gateway cannot be used in live mode")
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
