import pytest



def test_manual_gateway_conformance_test_mode(frappe):
    from saas_core.gateways.manual import ManualGateway
    gw = ManualGateway(extra={"mode": "test"})
    assert "client_callback" in gw.supports
    assert gw.client_payload(object()) == {"provider": "manual"}
    ev = gw.verify_client_callback({"tenant": "acme"})
    assert ev.kind == "checkout.completed" and ev.gateway == "manual" and ev.tenant_ref == "acme"


def test_manual_gateway_callback_refused_in_live_mode(frappe):
    from saas_core.gateways.base import GatewayError
    from saas_core.gateways.manual import ManualGateway
    with pytest.raises(GatewayError):
        ManualGateway(extra={"mode": "live"}).verify_client_callback({"tenant": "acme"})


@pytest.mark.parametrize("modname,clsname,name", [("stripe", "StripeGateway", "stripe")])
def test_stripe_conformance(frappe, modname, clsname, name):
    import importlib
    gw = getattr(importlib.import_module(f"saas_core.gateways.{modname}"), clsname)()
    assert isinstance(gw.supports, frozenset) and gw.supports
    from saas_core.gateways.base import CheckoutResult
    res = CheckoutResult(url="https://pay/x", session_ref="s")
    assert gw.client_payload(res) == {"provider": name, "url": "https://pay/x"}
    with pytest.raises(NotImplementedError, match="client callbacks"):
        gw.verify_client_callback({})
