import sys
import types

import pytest


@pytest.fixture
def env(frappe, monkeypatch):
    def only_for(role):
        if role not in getattr(frappe, "_roles", []):
            raise frappe.PermissionError("no")
    frappe.only_for = only_for
    frappe.session.user = "admin@x.com"
    logs = []
    audit = types.ModuleType("saas_core.audit")
    audit.log = lambda *a, **k: logs.append((a, k))
    monkeypatch.setitem(sys.modules, "saas_core.audit", audit)
    boot = types.ModuleType("saas_core.gateways.bootstrap")
    boot.from_site_config = lambda: {"account": "razorpay-test", "action": "created", "key_secret": "LEAK"}
    monkeypatch.setitem(sys.modules, "saas_core.gateways.bootstrap", boot)
    from saas_core.api.v1.admin import ops
    return frappe, ops, logs


def test_non_admin_403(env):
    frappe, ops, logs = env
    frappe._roles = ["SaaS Admin"]
    out = ops.run(op="sync_plans")
    assert out["error"]["code"] == "forbidden" and frappe.local.response["http_status_code"] == 403
    assert logs == []


def test_unknown_op_422(env):
    frappe, ops, _ = env
    frappe._roles = ["System Manager"]
    out = ops.run(op="rm_rf")
    assert out["error"]["code"] == "validation_error" and frappe.local.response["http_status_code"] == 422


def test_admin_ok_secret_free_and_audited(env):
    frappe, ops, logs = env
    frappe._roles = ["System Manager"]
    out = ops.run(op="bootstrap_gateway")
    assert out == {"op": "bootstrap_gateway", "result": {"account": "razorpay-test", "action": "created"}}
    assert logs[0][0] == ("admin.ops.run",) and logs[0][1]["details"] == {"op": "bootstrap_gateway"}


def test_scrub_key_fragments_and_value_patterns(env):
    _, ops, _ = env
    out = ops._scrub({"smtp_pass": "x", "pwd": "x", "api_key": "x", "auth_token": "x", "ok": "fine",
                      "a": "rzp_live_AbCd1234", "b": "sk_test_abcdef", "c": "whsec_abc123",
                      "d": "a" * 40, "e": "deadbeef" * 8, "n": [{"x": "rzp_test_zz"}], "i": 3})
    assert out == {"ok": "fine", "a": "[redacted]", "b": "[redacted]", "c": "[redacted]", "d": "[redacted]",
                   "e": "[redacted]", "n": [{"x": "[redacted]"}], "i": 3}
