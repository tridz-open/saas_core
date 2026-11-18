"""Checkout, subscription, invoices, payment methods: auth, ownership 404, idempotency, no secret leakage."""
import sys
import types

import pytest


def seed(frappe, doctype, **kw):
    return frappe.get_doc({"doctype": doctype, **kw}).insert()


class FakeGateway:
    def __init__(self): self.calls = []
    def create_checkout_session(self, params):
        self.calls.append(("checkout", params))
        return types.SimpleNamespace(session_ref="cs_1", url="https://pay.test/cs_1", raw={"public_key": "pk_test"}, payment_mocked=False)
    def portal_link(self, ref, return_url): self.calls.append(("portal", ref)); return "https://portal.test/x"
    def cancel_subscription(self, ref, at_end): self.calls.append(("cancel", ref, at_end))


@pytest.fixture
def env(frappe):
    gw = FakeGateway()
    reg = types.ModuleType("saas_core.gateways.registry"); reg.get_gateway = lambda name, account=None: gw
    ent = types.ModuleType("saas_core.billing.entitlements")
    ent.can_create_tenant = lambda user, plan: (True, "")
    ent.capacity_open = lambda: True
    perm = types.ModuleType("saas_core.permissions")

    def assert_tenant_access(t, user=None, ptype="read"):
        d = frappe._store.docs.get(("Tenant", t))
        if not d or d.get("owner_user") != user:
            raise frappe.DoesNotExistError(t)
        return d
    perm.assert_tenant_access = assert_tenant_access
    for n, m in (("saas_core.gateways.registry", reg), ("saas_core.billing.entitlements", ent), ("saas_core.permissions", perm)):
        sys.modules[n] = m
    frappe.session.user = "a@x.io"
    seed(frappe, "User", name="a@x.io", email="a@x.io")
    seed(frappe, "Plan", name="pro", code="pro", title="Pro", is_public=1,
         prices=[{"interval": "month", "amount": 900, "currency": "USD", "gateway": "stripe", "gateway_price_id": "price_1"}])
    seed(frappe, "Tenant", name="acme", slug="acme", owner_user="a@x.io", status="active", plan="pro")
    seed(frappe, "Tenant", name="theirs", slug="theirs", owner_user="b@x.io", status="active", plan="pro")
    seed(frappe, "Subscription", name="SUB-1", tenant="acme", owner_user="a@x.io", plan="pro", gateway="stripe",
         gateway_sub_id="sub_secret", gateway_customer_id="cus_secret", status="active", interval="month",
         creation=1)
    seed(frappe, "Subscription", name="SUB-2", tenant="theirs", owner_user="b@x.io", plan="pro", gateway="stripe",
         status="active", creation=2)
    seed(frappe, "Invoice", name="INV-1", tenant="acme", owner_user="a@x.io", status="paid", total_amount=900,
         currency="USD", pdf_url="https://pdf.test/1.pdf", gateway_invoice_id="in_secret", creation=1)
    seed(frappe, "Invoice", name="INV-2", tenant="theirs", owner_user="b@x.io", status="paid", total_amount=1,
         currency="USD", creation=2)
    return frappe, gw


REQ = dict(plan="pro", interval="month", success_url="https://app.test/ok", cancel_url="https://app.test/no")


def test_everything_requires_login(env):
    frappe, _ = env
    frappe.session.user = "Guest"
    from saas_core.api.v1 import checkout, subscription, invoices, payment_methods
    for fn in (lambda: checkout.create_session(**REQ, slug="newsite"), lambda: subscription.get("acme"),
               invoices.list, lambda: invoices.get("INV-1"), lambda: invoices.pdf("INV-1"),
               payment_methods.portal_link):
        with pytest.raises(frappe.PermissionError):
            fn()


def test_checkout_creates_pending_tenant_201(env):
    frappe, gw = env
    frappe.conf["saas_core_default_gateway"] = "stripe"
    from saas_core.api.v1 import checkout
    out = checkout.create_session(**REQ, slug="newsite")
    assert out == {"gateway": "stripe", "session_id": "cs_1", "url": "https://pay.test/cs_1",
                   "tenant": "newsite", "public_key": "pk_test", "payment_mocked": False}
    assert frappe.local.response["http_status_code"] == 201
    t = frappe.get_doc("Tenant", "newsite")
    assert t["status"] == "pending" and t["owner_user"] == "a@x.io"
    assert gw.calls[0][1].gateway_price_id == "price_1"


def test_checkout_validation_and_entitlements(env):
    frappe, _ = env
    from saas_core.api.v1 import checkout
    assert checkout.create_session(**REQ)["error"]["code"] == "bad_request"
    assert checkout.create_session(**REQ, slug="Bad Slug")["error"]["field_errors"]["slug"]
    assert checkout.create_session(**{**REQ, "plan": "nope"}, slug="newsite")["error"]["code"] == "not_found"
    assert checkout.create_session(**REQ, slug="acme")["error"]["code"] == "conflict"
    sys.modules["saas_core.billing.entitlements"].can_create_tenant = lambda u, p: (False, "capacity full")
    r = checkout.create_session(**REQ, slug="another")
    assert r["error"]["code"] == "capacity_unavailable" and frappe.local.response["http_status_code"] == 409
    assert not frappe.db.exists("Tenant", "another")


def test_checkout_other_users_tenant_404(env):
    frappe, _ = env
    from saas_core.api.v1 import checkout
    r = checkout.create_session(**REQ, tenant="theirs")
    assert r["error"]["code"] == "not_found" and frappe.local.response["http_status_code"] == 404


def test_checkout_idempotent(env):
    frappe, gw = env
    frappe.form_dict["idempotency_key"] = "ck1"
    from saas_core.api.v1 import checkout
    a = checkout.create_session(**REQ, slug="newsite")
    b = checkout.create_session(**REQ, slug="newsite")
    assert a == b and len(gw.calls) == 1
    r = checkout.create_session(**REQ, slug="different")
    assert r["error"]["code"] == "idempotency_mismatch"


def test_subscription_get_ownership(env):
    frappe, _ = env
    from saas_core.api.v1 import subscription
    out = subscription.get("acme")
    assert out["tenant"] == "acme" and out["status"] == "active"
    assert "sub_secret" not in str(out) and "cus_secret" not in str(out)
    r = subscription.get("theirs")
    assert r["error"]["code"] == "not_found" and frappe.local.response["http_status_code"] == 404
    assert subscription.get()["error"]["code"] == "bad_request"


def test_portal_link(env):
    frappe, gw = env
    from saas_core.api.v1 import payment_methods
    p = payment_methods.portal_link("acme", "https://app.test")
    assert p["url"] == "https://portal.test/x" and p["expires_at"]
    assert ("portal", "cus_secret") in gw.calls
    assert payment_methods.portal_link("theirs")["error"]["code"] == "not_found"


def test_legacy_subscription_cancel_and_portal_removed(env):
    from saas_core.api.v1 import subscription
    assert not hasattr(subscription, "cancel") and not hasattr(subscription, "portal_link")


def test_invoices(env):
    frappe, _ = env
    from saas_core.api.v1 import invoices
    out = invoices.list()
    assert [i["name"] for i in out["items"]] == ["INV-1"] and out["next_cursor"] is None
    assert out["items"][0]["total"] == {"amount": 900, "currency": "USD"} and "in_secret" not in str(out)
    assert invoices.list(tenant="theirs")["error"]["code"] == "not_found"
    assert invoices.get("INV-1")["has_pdf"] is True
    assert invoices.get("INV-2")["error"]["code"] == "not_found"
    assert invoices.get("NOPE")["error"]["code"] == "not_found"
    assert invoices.pdf("INV-1") is None
    assert frappe.local.response["type"] == "redirect" and frappe.local.response["location"].endswith("1.pdf")
    assert invoices.pdf("INV-2")["error"]["code"] == "not_found"
    seed(frappe, "Invoice", name="INV-3", tenant="acme", owner_user="a@x.io", status="paid", total_amount=1,
         currency="USD", pdf_url="", creation=3)
    assert invoices.pdf("INV-3")["error"]["code"] == "not_available"


def test_invoices_pagination(env):
    frappe, _ = env
    for i in range(3):
        seed(frappe, "Invoice", name=f"INV-X{i}", tenant="acme", owner_user="a@x.io", status="open",
             total_amount=1, currency="USD", creation=10 + i)
    from saas_core.api.v1 import invoices
    p1 = invoices.list(limit=2)
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    p2 = invoices.list(limit=2, cursor=p1["next_cursor"])
    assert len(p2["items"]) == 2 and p2["next_cursor"] is None


def test_payment_methods_portal(env):
    frappe, gw = env
    from saas_core.api.v1 import payment_methods
    assert payment_methods.portal_link()["url"] == "https://portal.test/x"
    assert payment_methods.portal_link(tenant="acme")["url"]
    assert payment_methods.portal_link(tenant="theirs")["error"]["code"] == "not_found"
