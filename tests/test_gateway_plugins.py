"""Pluggable gateways: hook registration + validation, experimental gating of `fake`, capability checks in billing,
and the generic webhook receiver."""
import datetime as dt
import json
import sys
import types

import pytest

import importlib


class _Lazy:
    """conftest evicts saas_core modules per test, so resolve them at use time, not import time."""
    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, k):
        return getattr(importlib.import_module(self._mod), k)


fake_mod = _Lazy("saas_core.gateways.fake")

NOW = dt.datetime(2026, 11, 18, 6, 0)
WH = "whsec_plugin"


def hooks(frappe, mapping, target=None):
    frappe.conf["_test_hooks"] = {"saas_core_gateways": mapping}
    if target is not None:  # make dotted paths resolve
        frappe.get_attr = lambda path: target[path]


def account(frappe, gateway, mode="test", name="a1"):
    doc = frappe._Doc(doctype="Gateway Account", name=name, gateway=gateway, enabled=1, key_id="kid", mode=mode)
    doc.get_password = lambda f, raise_exception=True: {"key_secret": "KS", "webhook_secret": WH}[f]
    doc.insert()


def make_plugin(**attrs):
    return type("PlugGw", (fake_mod.FakeGateway,), {"name": "plug", **attrs})


# ---- registry -------------------------------------------------------------------------------------------
def test_hook_registers_plugin_and_builds_it(frappe):
    from saas_core.gateways import registry
    cls = make_plugin()
    hooks(frappe, {"plug": "my_app.gw.Plug"}, {"my_app.gw.Plug": cls})
    account(frappe, "plug")
    assert "plug" in registry.available_gateways()
    gw = registry.get_gateway("plug")
    assert isinstance(gw, cls) and gw._webhook_secret == WH


def test_hook_value_as_list_of_dicts_is_merged(frappe):
    from saas_core.gateways import registry
    cls = make_plugin()
    frappe.conf["_test_hooks"] = {"saas_core_gateways": [{"plug": "a.B"}, {"other": "c.D"}]}
    frappe.get_attr = lambda path: cls
    assert registry.gateway_class("plug") is cls


def _bad(kind):
    from saas_core.gateways.base import Gateway
    F = fake_mod.FakeGateway
    return {
        "NotGw": lambda: type("NotGw", (), {"name": "plug", "supports": frozenset()}),
        "NoName": lambda: type("NoName", (Gateway,), {"supports": frozenset()}),
        "Mismatch": lambda: type("Mismatch", (F,), {"name": "other"}),
        "BadSup": lambda: type("BadSup", (F,), {"name": "plug", "supports": frozenset({"teleport"})}),
        "ListSup": lambda: type("ListSup", (F,), {"name": "plug", "supports": ["refund"]}),
    }[kind]()


@pytest.mark.parametrize("kind,msg", [
    ("NotGw", "must subclass"), ("NoName", "non-empty"), ("Mismatch", "must equal the hook key"),
    ("BadSup", "unknown capabilities"), ("ListSup", "frozenset"),
])
def test_invalid_plugin_classes_are_refused(frappe, kind, msg):
    from saas_core.gateways import registry
    hooks(frappe, {"plug": "x.Y"}, {"x.Y": _bad(kind)})
    with pytest.raises(frappe.ValidationError, match=msg):
        registry.gateway_class("plug")


def test_incomplete_plugin_with_abstract_methods_is_refused(frappe):
    from saas_core.gateways import registry
    from saas_core.gateways.base import Gateway
    Half = type("Half", (Gateway,), {"name": "plug", "supports": frozenset()})
    hooks(frappe, {"plug": "x.Y"}, {"x.Y": Half})
    with pytest.raises(frappe.ValidationError, match="does not implement"):
        registry.gateway_class("plug")


def test_plugin_cannot_replace_builtin(frappe):
    from saas_core.gateways import registry
    hooks(frappe, {"razorpay": "x.Y"}, {"x.Y": make_plugin(name="razorpay")})
    assert registry.gateway_class("razorpay") is registry.GATEWAYS["razorpay"]  # built-ins win; hook ignored
    with pytest.raises(frappe.ValidationError, match="cannot replace"):
        registry._load_plugin("razorpay", make_plugin(name="razorpay"))


def test_unimportable_plugin_gives_clear_error(frappe):
    from saas_core.gateways import registry
    hooks(frappe, {"plug": "missing.Mod"}, {})
    with pytest.raises(frappe.ValidationError, match="cannot import"):
        registry.gateway_class("plug")


def test_unknown_gateway(frappe):
    from saas_core.gateways import registry
    with pytest.raises(frappe.ValidationError, match="Unknown gateway"):
        registry.gateway_class("nope")


def test_plugin_can_opt_into_experimental_gate(frappe):
    from saas_core.gateways import registry
    cls = make_plugin(experimental_feature="dunning")
    hooks(frappe, {"plug": "x.Y"}, {"x.Y": cls})
    account(frappe, "plug")
    frappe.conf["saas_core_experimental"] = ""
    with pytest.raises(frappe.ValidationError, match="experimental"):
        registry.get_gateway("plug")
    frappe.conf["saas_core_experimental"] = "dunning"
    assert registry.get_gateway("plug")


# ---- fake gateway gating ------------------------------------------------------------------------------
@pytest.mark.strict_experimental
def test_fake_gateway_requires_experimental_flag(frappe):
    from saas_core.gateways import registry
    account(frappe, "fake")
    with pytest.raises(frappe.ValidationError, match="fake_gateway"):
        registry.get_gateway("fake")
    frappe.conf["saas_core_experimental"] = "fake_gateway"
    assert registry.get_gateway("fake").name == "fake"


def test_fake_gateway_refused_in_live_mode(frappe):
    from saas_core.gateways import registry
    account(frappe, "fake", mode="live")
    with pytest.raises(frappe.ValidationError, match="live mode"):
        registry.get_gateway("fake")


# ---- generic webhook receiver ----------------------------------------------------------------------------
def test_generic_webhook_pipeline_for_plugin_gateway(frappe):
    import importlib
    webhooks = importlib.import_module("saas_core.webhooks")
    fake_mod.reset()
    account(frappe, "fake")
    body = json.dumps({"id": "evt_1", "type": "payment.succeeded", "subscription": "sub_1", "amount": 4900,
                       "currency": "USD"}).encode()
    headers = fake_mod.sign_webhook(WH, body)
    assert webhooks.ingest("fake", headers, body) == {"status": "accepted"}
    assert webhooks.ingest("fake", headers, body) == {"status": "duplicate"}
    with pytest.raises(frappe.AuthenticationError):
        webhooks.ingest("fake", {"x-fake-signature": "00"}, body)


@pytest.mark.parametrize("name", ["", "../etc", "Bad Name", "a" * 40, "9x"])
def test_receive_rejects_malformed_gateway_names(frappe, name):
    import importlib
    webhooks = importlib.import_module("saas_core.webhooks")
    with pytest.raises(frappe.ValidationError):
        webhooks.receive(name)


# ---- billing checks capabilities, not attributes ----------------------------------------------------------
def _refund_env(frappe, monkeypatch):
    em = types.ModuleType("saas_core.events")
    em.publish = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "saas_core.events", em)
    for n, kw in (("Plan", dict(name="pro", code="pro")),
                  ("Tenant", dict(name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")),
                  ("Subscription", dict(name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway="stripe",
                                        status="active", refunded_amount=0, gateway_payment_id="pay_1"))):
        frappe.get_doc({"doctype": n, **kw}).insert()
    return frappe.get_doc("Subscription", "SUB-1")


def test_refund_on_gateway_without_refund_capability_fails_cleanly(frappe, monkeypatch):
    from saas_core.billing import refunds
    from saas_core.gateways.stripe import StripeGateway
    sub = _refund_env(frappe, monkeypatch)
    doc = refunds.create_doc(sub, user="u@x.com", kind="cancel", amount=1000, currency="USD", payment_id="pay_1",
                             reason="t", now=NOW)
    assert refunds.issue(doc, sub, StripeGateway(), NOW) is False
    assert doc.status == "failed" and "does not support refunds" in doc.error
    assert "attempt 5" in doc.error  # not retried forever
    assert "AttributeError" not in doc.error


def test_refund_retry_without_lookup_capability_never_creates(frappe, monkeypatch):
    """A real Gateway that can refund but cannot look refunds up must not re-create on retry."""
    from saas_core.billing import refunds
    from saas_core.gateways.base import Gateway
    calls = []

    class NoLookup(fake_mod.FakeGateway):
        name = "nolookup"
        supports = fake_mod.FakeGateway.supports - {"refund_lookup", "payment_lookup"}
        list_payment_refunds = Gateway.list_payment_refunds
        get_payment = Gateway.get_payment

        def refund_payment(self, *a, **k):
            calls.append(a)
            raise TimeoutError("lost response")

    sub = _refund_env(frappe, monkeypatch)
    doc = refunds.create_doc(sub, user="u@x.com", kind="cancel", amount=1000, currency="USD", payment_id="pay_1",
                             reason="t", now=NOW)
    gw = NoLookup()
    assert refunds.issue(doc, sub, gw, NOW) is False and len(calls) == 1   # first attempt creates
    assert refunds.issue(doc, sub, gw, NOW) is False and len(calls) == 1   # retry refuses to create blindly
    assert "refund_lookup" in doc.error


def test_require_capability_error_is_a_gateway_error():
    from saas_core.gateways.base import GatewayCapabilityError, GatewayError, require_capability
    from saas_core.gateways.stripe import StripeGateway
    with pytest.raises(GatewayError) as ei:
        require_capability(StripeGateway(), "refund")
    assert isinstance(ei.value, GatewayCapabilityError) and ei.value.code == "capability_unsupported"
    assert "stripe" in str(ei.value) and "refund" in str(ei.value)
    require_capability(StripeGateway(), "subscription")  # declared: no error


def test_base_defaults_raise_gateway_error_not_attribute_error():
    from saas_core.gateways.base import GatewayError
    from saas_core.gateways.stripe import StripeGateway
    g = StripeGateway()
    for call in (lambda: g.refund_payment("p", 100), lambda: g.get_payment("p"), lambda: g.list_payment_refunds("p"),
                 lambda: g.update_subscription_plan("s", "p"), lambda: g.invoice_payment_id("i"),
                 lambda: g.create_payment_link(amount=1, currency="USD", description="d", email=None),
                 lambda: g.verify_client_callback({})):
        with pytest.raises(GatewayError):
            call()


def test_every_capability_has_a_description_and_known_method():
    from saas_core.gateways.base import CAPABILITIES, CAPABILITY_METHODS, Gateway
    assert all(isinstance(v, str) and v for v in CAPABILITIES.values())
    assert set(CAPABILITY_METHODS) <= set(CAPABILITIES)
    assert all(callable(getattr(Gateway, m)) for m in CAPABILITY_METHODS.values())
