"""billing.preview_cancel / cancel / preview_change: auth, ownership, conflicts, idempotency, gateway errors."""
import datetime as dt
import sys
import types

import pytest

from test_billing_cancellation import END, START, FakeGW, mk


@pytest.fixture
def env(frappe, monkeypatch):
    gw = FakeGW()
    reg = types.ModuleType("saas_core.gateways.registry")
    reg.get_gateway = lambda name=None, account=None: gw
    perm = types.ModuleType("saas_core.permissions")
    perm.calls = []

    def assert_tenant_access(t, user=None, ptype="read"):
        perm.calls.append((t, user, ptype))
        d = frappe._store.docs.get(("Tenant", t))
        if not d or d.get("owner_user") != user or (ptype == "write" and user == "viewer@x.io"):
            raise frappe.DoesNotExistError(t)
        return d
    perm.assert_tenant_access = assert_tenant_access
    am = types.ModuleType("saas_core.audit"); am.log = lambda *a, **k: None
    em = types.ModuleType("saas_core.events"); em.publish = lambda *a, **k: None
    for n, m in (("saas_core.gateways.registry", reg), ("saas_core.permissions", perm), ("saas_core.audit", am),
                 ("saas_core.events", em)):
        monkeypatch.setitem(sys.modules, n, m)
    frappe.session.user = "u@x.com"
    mk(frappe, "Plan", name="pro", code="pro", title="Pro")
    mk(frappe, "Tenant", name="acme", slug="acme", owner_user="u@x.com", status="active", plan="pro")
    mk(frappe, "Subscription", name="SUB-1", tenant="acme", owner_user="u@x.com", plan="pro", gateway="razorpay",
       gateway_sub_id="sub_1", status="active", interval="month", currency="INR", period_start=START,
       period_end=END, paid_amount=3100, gateway_payment_id="pay_1", creation=1)
    return types.SimpleNamespace(f=frappe, gw=gw, perm=perm)


def test_guest_rejected(env):
    from saas_core.api.v1 import billing
    env.f.session.user = "Guest"
    for fn in (lambda: billing.preview_cancel("acme"), lambda: billing.cancel("acme"),
               lambda: billing.preview_change("acme", "pro")):
        with pytest.raises(env.f.PermissionError):
            fn()


def test_preview_cancel_is_read_only_and_get(env):
    from saas_core.api.v1 import billing, _common
    assert "billing.preview_cancel" in _common.READ_ENDPOINTS and "billing.preview_change" in _common.READ_ENDPOINTS
    before = dict(env.f.get_doc("Subscription", "SUB-1"))
    out = billing.preview_cancel("acme")
    assert out["tenant"] == "acme" and out["can_cancel"] is True and out["refund_to"] == "original payment method"
    assert "refund_minor" in out and "stops_at" in out
    assert dict(env.f.get_doc("Subscription", "SUB-1")) == before and env.gw.calls == []
    assert env.perm.calls[-1][2] == "read"


def test_other_users_tenant_404_and_no_write(env):
    from saas_core.api.v1 import billing
    env.f.session.user = "b@x.io"
    assert billing.preview_cancel("acme")["error"]["code"] == "not_found"
    assert billing.cancel("acme")["error"]["code"] == "not_found"
    assert env.gw.calls == []


def test_cancel_requires_write_permission(env):
    from saas_core.api.v1 import billing
    env.f._store.docs[("Tenant", "acme")]["owner_user"] = "viewer@x.io"
    env.f.session.user = "viewer@x.io"
    out = billing.cancel("acme")
    assert out["error"]["code"] == "not_found" and env.perm.calls[-1][2] == "write" and env.gw.calls == []


def test_cancel_ok_shape_and_missing_tenant(env):
    from saas_core.api.v1 import billing
    out = billing.cancel("acme", reason="x")
    assert out["status"] == "canceling" and out["can_cancel"] is False and "refund" in out
    assert out["tenant"] == "acme" and env.f.get_doc("Subscription", "SUB-1")["cancel_effective_at"]
    assert billing.cancel()["error"]["code"] == "bad_request"


def test_already_canceling_409(env):
    from saas_core.api.v1 import billing
    billing.cancel("acme")
    out = billing.cancel("acme")
    assert out["error"]["code"] == "already_canceling" and env.f.local.response["http_status_code"] == 409


def test_plan_change_pending_409(env):
    from saas_core.api.v1 import billing
    env.f.get_doc("Subscription", "SUB-1")["plan_change_state"] = "scheduled"
    out = billing.cancel("acme")
    assert out["error"]["code"] == "plan_change_pending" and env.f.local.response["http_status_code"] == 409
    assert env.gw.calls == []


def test_gateway_failure_502_state_untouched(env):
    from saas_core.api.v1 import billing
    env.gw.fail_cancel = True
    out = billing.cancel("acme")
    assert out["error"]["code"] == "gateway_error" and env.f.local.response["http_status_code"] == 502
    assert not env.f.get_doc("Subscription", "SUB-1").get("cancel_effective_at")


def test_idempotent_replay_and_mismatch(env):
    from saas_core.api.v1 import billing
    env.f.form_dict["idempotency_key"] = "k1"
    a = billing.cancel("acme", reason="r")
    b = billing.cancel("acme", reason="r")
    assert a == b and [c[0] for c in env.gw.calls].count("cancel") == 1
    c = billing.cancel("acme", reason="different")
    assert c["error"]["code"] == "idempotency_mismatch"


def test_preview_change_delegates_to_plan_change(env, monkeypatch):
    from saas_core.api.v1 import billing
    seen = []
    pc = types.ModuleType("saas_core.billing.plan_change")
    pc.preview = lambda sub, plan, now=None: (seen.append((sub["name"], plan)) or {"tenant": "acme", "to_plan": plan})
    monkeypatch.setitem(sys.modules, "saas_core.billing.plan_change", pc)
    import saas_core.billing as pkg
    monkeypatch.setattr(pkg, "plan_change", pc, raising=False)
    assert billing.preview_change("acme", "pro") == {"tenant": "acme", "to_plan": "pro"}
    assert seen == [("SUB-1", "pro")]
    assert billing.preview_change("acme", "nope")["error"]["code"] == "not_found"
    assert billing.preview_change("acme")["error"]["code"] == "bad_request"
