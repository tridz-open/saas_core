"""checkout.verify_payment: bad sig 401, good sig 200 + one Payment Event, replay is a no-op."""
import hashlib, hmac, sys, types, importlib

import pytest

SECRET = "key_secret_xyz"


def sign(pid, sub):
    return hmac.new(SECRET.encode(), f"{pid}|{sub}".encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def env(frappe):
    from saas_core.gateways.razorpay import RazorpayGateway
    gw = RazorpayGateway(key_id="rzp_test_abc", key_secret=SECRET, webhook_secret="wh", http=None)
    reg = types.ModuleType("saas_core.gateways.registry"); reg.get_gateway = lambda name, account=None: gw
    perm = types.ModuleType("saas_core.permissions")

    def assert_tenant_access(t, user=None, ptype="read"):
        d = frappe._store.docs.get(("Tenant", t))
        if not d or d.get("owner_user") != user:
            raise frappe.DoesNotExistError(t)
        return d
    perm.assert_tenant_access = assert_tenant_access
    sys.modules["saas_core.gateways.registry"] = reg
    sys.modules["saas_core.permissions"] = perm
    frappe.session.user = "a@x.io"
    frappe.get_doc({"doctype": "Tenant", "name": "acme", "owner_user": "a@x.io", "status": "pending"}).insert()
    frappe.get_doc({"doctype": "Tenant", "name": "theirs", "owner_user": "b@x.io", "status": "pending"}).insert()
    # create_session persists these; verify_payment takes the tenant from here, never from client notes
    from saas_core.billing.checkout_sessions import store_session
    store_session("razorpay", "sub_1", tenant="acme", owner_user="a@x.io", plan="pro", interval="month",
                  billing_mode="subscription")
    store_session("razorpay", "sub_theirs", tenant="theirs", owner_user="b@x.io", plan="pro", interval="month",
                  billing_mode="subscription")
    return importlib.import_module("saas_core.api.v1.checkout_verify")


def payload(tenant="acme", pid="pay_1", sub="sub_1", sig=None):
    sub = "sub_theirs" if tenant == "theirs" else sub
    return {"razorpay_payment_id": pid, "razorpay_subscription_id": sub,
            "razorpay_signature": sig or sign(pid, sub), "notes": {"tenant": tenant}}


def events(frappe):
    return [d for (dt, _), d in frappe._store.docs.items() if dt == "Payment Event"]


def test_bad_signature_401(frappe, env):
    r = env.verify_payment("razorpay", payload(sig="0" * 64))
    assert frappe.local.response["http_status_code"] == 401
    assert r["error"]["code"] == "invalid_signature"
    assert SECRET not in str(r)
    assert events(frappe) == [] and frappe._store.enqueued == []


def test_good_signature_creates_one_event(frappe, env):
    assert env.verify_payment("razorpay", payload()) == {"status": "accepted"}
    evs = events(frappe)
    assert len(evs) == 1 and evs[0].name == "razorpay:pay_1" and evs[0].kind == "checkout.completed"
    assert evs[0].tenant_ref == "acme"
    assert frappe._store.enqueued == [("saas_core.billing.processor.process_payment_event", {"payment_event": "razorpay:pay_1", "enqueue_after_commit": True})]


def test_replay_is_noop(frappe, env):
    env.verify_payment("razorpay", payload())
    events(frappe)[0].status = "processed"  # an unprocessed same-key event is NOT a duplicate (see race tests)
    assert env.verify_payment("razorpay", payload()) == {"status": "duplicate"}
    assert len(events(frappe)) == 1 and len(frappe._store.enqueued) == 1


def test_other_users_tenant_404(frappe, env):
    r = env.verify_payment("razorpay", payload(tenant="theirs"))
    assert r["error"]["code"] == "not_found"
    assert events(frappe) == []


def test_registered_in_package(frappe, env):
    from saas_core.api import v1
    assert v1.checkout_verify.verify_payment
