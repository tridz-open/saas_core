"""checkout.create_session: metadata mode/amount_minor are server-derived; client_payload passthrough."""
import sys
import types

import pytest


def seed(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


class GW:
    def __init__(self, payload="unsupported"):
        self.params, self.payload = None, payload

    def create_checkout_session(self, params):
        self.params = params
        return types.SimpleNamespace(session_ref="order_1", url="", raw={}, payment_mocked=False)

    def client_payload(self, res):
        if self.payload == "unsupported":
            raise NotImplementedError
        return self.payload


@pytest.fixture
def env(frappe):
    holder = types.SimpleNamespace(gw=GW())
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name, account=None: holder.gw
    ent = types.ModuleType("saas_core.billing.entitlements")
    ent.can_create_tenant = lambda user, plan: (True, "")
    ent.capacity_open = lambda: True
    sys.modules["saas_core.gateways.registry"] = reg
    sys.modules["saas_core.billing.entitlements"] = ent
    frappe.session.user = "a@x.io"
    seed(frappe, "User", name="a@x.io", email="a@x.io")
    seed(frappe, "Plan", name="pro", code="pro", title="Pro", is_public=1,
         prices=[{"interval": "month", "amount": 49950, "currency": "INR", "gateway": "razorpay",
                  "gateway_price_id": "plan_1"}])
    return frappe, holder


REQ = dict(plan="pro", interval="month", success_url="https://app.test/ok", cancel_url="https://app.test/no",
           gateway="razorpay")


def test_metadata_amount_from_server_default_mode(env):
    _, h = env
    from saas_core.api.v1 import checkout
    checkout.create_session(**REQ, slug="site-one", recipe_fields={"amount_minor": 1})
    assert h.gw.params.metadata["amount_minor"] == 49950
    assert h.gw.params.metadata["mode"] == "order"


def test_mode_from_gateway_account(env):
    frappe, h = env
    seed(frappe, "Gateway Account", name="rz", account_name="rz", gateway="razorpay", enabled=1,
         checkout_mode="order")
    from saas_core.api.v1 import checkout
    checkout.create_session(**REQ, slug="site-two")
    assert h.gw.params.metadata["mode"] == "order"


def test_client_payload_included_when_supported(env):
    _, h = env
    h.gw.payload = {"provider": "razorpay", "key": "rzp_test_x", "mode": "order", "order_id": "order_1"}
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, slug="site-three")
    assert out["client_payload"]["order_id"] == "order_1"


def test_client_payload_absent_when_unsupported(env):
    _, h = env
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, slug="site-four")
    assert "client_payload" not in out and out["session_id"] == "order_1"


def test_verify_payment_reexported(env):
    from saas_core.api.v1 import checkout, checkout_verify
    assert checkout.verify_payment is checkout_verify.verify_payment
