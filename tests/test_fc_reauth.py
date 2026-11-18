import importlib
import sys
import types

import pytest


@pytest.fixture
def env(frappe, monkeypatch):
    for k, v in {"saas_core_fc_email": "bot@x.io", "saas_core_fc_imap_host": "imap.x.io",
                 "saas_core_fc_imap_user": "bot", "saas_core_fc_imap_password": "s3cret-pw"}.items():
        frappe.conf[k] = v
    import fcctl.auth as fa
    calls = types.SimpleNamespace(login=0, poller=None)

    class Poller:
        def __init__(self, host, user, password, **kw):
            calls.poller = (host, user, password)

    def login(central, email, provider):
        calls.login += 1
        if getattr(calls, "fail", False):
            raise RuntimeError("boom s3cret-pw")
        central.store.set_central(sid="new-sid", csrf="new-csrf", email=email)

    monkeypatch.setattr(fa, "ImapCodePoller", Poller)
    monkeypatch.setattr(fa, "login_with_code", login)
    audits = []
    aud = importlib.import_module("saas_core.audit")
    monkeypatch.setattr(aud, "log", lambda action, **kw: audits.append((action, kw)) or "A")
    return types.SimpleNamespace(frappe=frappe, calls=calls, audits=audits, mod=importlib.import_module("saas_core.fc_reauth"))


def _outcomes(env):
    return [kw["details"]["outcome"] for _, kw in env.audits]


def test_success_shape_and_audit(env):
    out = env.mod.imap_reauther({"sid": "old"})
    assert out == {"sid": "new-sid", "csrf": "new-csrf", "email": "bot@x.io"}
    assert env.calls.poller == ("imap.x.io", "bot", "s3cret-pw")
    assert _outcomes(env) == ["success"]
    assert "s3cret-pw" not in repr(env.audits) and "new-sid" not in repr(env.audits)


def test_cooldown_blocks_second_attempt(env):
    assert env.mod.imap_reauther({"sid": "old"})
    assert env.mod.imap_reauther({"sid": "old"}) is None
    assert env.calls.login == 1 and _outcomes(env) == ["success", "cooldown"]


def test_cooldown_expires(env):
    env.mod.imap_reauther({"sid": "old"})
    env.frappe.cache().set_value(env.mod.K_LAST_ATTEMPT, 1.0)
    assert env.mod.imap_reauther({"sid": "old"})
    assert env.calls.login == 2


def test_cooldown_reuses_session_saved_by_other_worker(env, monkeypatch):
    env.frappe.cache().set_value(env.mod.K_LAST_ATTEMPT, __import__("time").time())
    jobs = importlib.import_module("saas_core.jobs")
    monkeypatch.setattr(jobs, "fc_load_session", lambda: {"sid": "other", "csrf": "c", "email": "bot@x.io"})
    assert env.mod.imap_reauther({"sid": "old"})["sid"] == "other"
    assert env.calls.login == 0 and _outcomes(env) == ["reused_fresh_session"]


def test_lock_held_returns_none(env, monkeypatch):
    aud = importlib.import_module("saas_core.audit")
    monkeypatch.setattr(env.mod, "LOCK_WAIT", 0.05)
    env.frappe.cache().setnx(f"{getattr(env.frappe.local, 'site', None) or ''}:{env.mod.LOCK_KEY}", "held")
    assert env.mod.imap_reauther({"sid": "old"}) is None
    assert env.calls.login == 0 and _outcomes(env) == ["lock_timeout"]


def test_failure_returns_none_without_leaking(env):
    env.calls.fail = True
    assert env.mod.imap_reauther({"sid": "old"}) is None
    assert _outcomes(env) == ["failed"] and "s3cret" not in repr(env.audits)


def test_missing_config_returns_none(env):
    env.frappe.conf.pop("saas_core_fc_imap_password")
    assert env.mod.imap_reauther({}) is None
    assert env.calls.login == 0 and _outcomes(env) == ["not_configured"]
