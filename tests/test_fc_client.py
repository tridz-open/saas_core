import importlib
import sys
import types

import pytest


@pytest.fixture
def env(frappe, monkeypatch, tmp_path):
    frappe.conf.update({"saas_core_fc_team": "TEAM-1", "saas_core_fc_server": "srv-1",
                        "saas_core_session_key": "k", "saas_core_fc_journal_dir": str(tmp_path / "j")})
    jobs = importlib.import_module("saas_core.jobs")
    saved = {}
    monkeypatch.setattr(jobs, "fc_load_session", lambda: dict(saved) if saved.get("sid") else {"sid": "S1", "csrf": "C1", "email": "b@x.io"})
    monkeypatch.setattr(jobs, "fc_save_session", lambda s: saved.update(s))

    class FakeClient:
        def __init__(self, store, team=None, **kw):
            self.store, self.team = store, team
            self.central = types.SimpleNamespace(store=store, team=team, reauth=None, call=lambda *a, **k: "ok")
            self.closed = False

        def pilot(self, server):
            return ("pilot", server)

        def close(self):
            self.closed = True

    fc = types.ModuleType("fcctl")
    fc.Client = FakeClient
    monkeypatch.setitem(sys.modules, "fcctl", fc)
    req = types.ModuleType("fcctl.request")

    class ReauthFailed(Exception):
        pass
    req.ReauthFailed = ReauthFailed
    monkeypatch.setitem(sys.modules, "fcctl.request", req)
    auth = types.ModuleType("fcctl.auth")
    auth.my_profile = lambda central: {"email": "b@x.io", "sid": "LEAK"}
    st = types.ModuleType("fcctl.sites")
    st.wildcard_domains = lambda p: {"domains": ["*.frappe.cloud"]}
    jr = types.ModuleType("fcctl.journal")
    jr.Journal = lambda d: ("journal", str(d))
    for n, m in (("auth", auth), ("sites", st), ("journal", jr)):
        monkeypatch.setitem(sys.modules, f"fcctl.{n}", m)
        setattr(fc, n, m)
    mod = importlib.import_module("saas_core.provisioning.fc_client")
    return types.SimpleNamespace(frappe=frappe, mod=mod, jobs=jobs, saved=saved, auth=auth, req=req)


def test_factory_uses_db_session_and_team(env):
    c = env.mod.build_fcctl_client()
    assert c.team == "TEAM-1" and c.central.store.sid == "S1" and c.central.store.csrf == "C1"
    assert isinstance(c.central.reauth, env.mod.ReauthBridge)


def test_reauth_bridge_renews_and_persists(env, monkeypatch):
    monkeypatch.setattr(env.jobs, "_fc_reauth", lambda s: {"sid": "S2", "csrf": "C2", "email": "b@x.io"})
    c = env.mod.build_fcctl_client()
    assert c.central.reauth.recover() == "reauther"
    assert c.central.store.sid == "S2" and env.saved["sid"] == "S2"


def test_reauth_bridge_failure_raises_secret_free(env, monkeypatch):
    monkeypatch.setattr(env.jobs, "_fc_reauth", lambda s: None)
    c = env.mod.build_fcctl_client()
    with pytest.raises(env.req.ReauthFailed) as e:
        c.central.reauth.recover()
    assert "S1" not in str(e.value)


def test_get_port_fc_mode(env):
    runner = importlib.import_module("saas_core.provisioning.runner")
    runner.set_port(None)
    p = runner.get_port()
    assert type(p).__name__ == "FcctlAdapter" and p._client.team == "TEAM-1" and p.server == "srv-1"
    assert p._journal == ("journal", str(env.frappe.conf["saas_core_fc_journal_dir"]))
    runner.set_port(None)


@pytest.mark.parametrize("missing", ["saas_core_fc_team", "saas_core_session_key"])
def test_missing_config_errors_clearly(env, missing):
    env.frappe.conf.pop(missing)
    runner = importlib.import_module("saas_core.provisioning.runner")
    runner.set_port(None)
    with pytest.raises(env.mod.FcConfigError) as e:
        runner.get_port()
    assert missing in str(e.value)


def test_fc_check_shape_no_secrets(env):
    out = env.mod.fc_check()
    assert out == {"authenticated": True, "email": "b@x.io", "team": "TEAM-1", "server": "srv-1",
                   "wildcard_domains": ["*.frappe.cloud"], "template_warnings": {}}
    assert "LEAK" not in repr(out)


def test_fc_check_auth_failure_reports_class_only(env):
    def boom(c):
        raise RuntimeError("sid=LEAK")
    env.auth.my_profile = boom
    out = env.mod.fc_check()
    assert out["authenticated"] is False and out["error"] == "RuntimeError" and "LEAK" not in repr(out)


def test_fc_check_op_role_and_shape(env, monkeypatch):
    frappe = env.frappe
    frappe.session.user = "a@x.com"
    frappe._roles = ["System Manager"]
    frappe.only_for = lambda role: (_ for _ in ()).throw(frappe.PermissionError("no")) if role not in frappe._roles else None
    ops = importlib.import_module("saas_core.api.v1.admin.ops")
    assert ops.run(op="fc_check")["error"]["code"] == "forbidden"
    frappe._roles = ["SaaS Admin"]
    out = ops.run(op="fc_check")
    assert out["op"] == "fc_check" and out["result"]["team"] == "TEAM-1" and out["result"]["authenticated"] is True


class ApiError(Exception):
    def __init__(self, msg, status=None, body=None):
        super().__init__(msg)
        self.status, self.body = status, body


GUEST = dict(msg="403 PermissionError You are not permitted to access this resource. Login to access x is not whitelisted",
             status=403, body={"exc_type": "PermissionError"})


def _wired(env, errors, loads, mp):
    st = {"reauth": 0, "calls": 0}
    sess = {"d": loads}
    mp.setattr(env.jobs, "fc_load_session", lambda: sess["d"])
    mp.setattr(env.jobs, "fc_save_session", lambda s: (env.saved.update(s), sess.update(d=dict(s))))

    def reauth(old):
        st["reauth"] += 1
        new = {"sid": "S2", "csrf": "C2", "email": "b@x.io"}
        env.jobs.fc_save_session(new)
        return new
    mp.setattr(env.jobs, "_fc_reauth", reauth)
    store = env.mod.DbSessionStore()
    bridge = env.mod.ReauthBridge(store)
    q = list(errors)

    def orig(*a, **kw):
        st["calls"] += 1
        if q:
            raise q.pop(0)
        return store.sid
    holder = types.SimpleNamespace(central=types.SimpleNamespace(call=orig))
    env.mod.install_guest_reauth(holder, bridge)
    return holder, st, store


def test_guest_403_triggers_one_reauth_and_retry(env, monkeypatch):
    h, st, store = _wired(env, [ApiError(**GUEST)], {"sid": "S1", "csrf": "C1"}, monkeypatch)
    assert h.central.call("GET", "x") == "S2"
    assert st == {"reauth": 1, "calls": 2} and env.saved["sid"] == "S2" and store.sid == "S2"


def test_no_session_logs_in_proactively(env, monkeypatch):
    h, st, store = _wired(env, [], None, monkeypatch)
    assert h.central.call("GET", "x") == "S2"
    assert st == {"reauth": 1, "calls": 1}


def test_genuine_403_with_session_not_retried(env, monkeypatch):
    err = ApiError("403 PermissionError no access to Site", 403, {"exc_type": "PermissionError"})
    h, st, _ = _wired(env, [err], {"sid": "S1"}, monkeypatch)
    with pytest.raises(ApiError):
        h.central.call("GET", "x")
    assert st == {"reauth": 0, "calls": 1}


def test_guest_after_retry_surfaces_without_loop(env, monkeypatch):
    h, st, _ = _wired(env, [ApiError(**GUEST), ApiError(**GUEST)], {"sid": "S1"}, monkeypatch)
    with pytest.raises(ApiError):
        h.central.call("GET", "x")
    assert st == {"reauth": 1, "calls": 2}


def test_fc_check_ensures_session_and_authenticates(env, monkeypatch):
    monkeypatch.setattr(env.jobs, "fc_load_session", lambda: None)
    monkeypatch.setattr(env.jobs, "_fc_reauth", lambda s: {"sid": "S2", "csrf": "C2", "email": "b@x.io"})
    out = env.mod.fc_check()
    assert out["authenticated"] is True and out["wildcard_domains"] == ["*.frappe.cloud"]
    assert "LEAK" not in str(out)


def test_jobs_fc_reauth_persists(env, monkeypatch):
    saved = []
    monkeypatch.setitem(env.frappe.conf, "saas_core_fc_reauther", "tests.test_fc_client._fake_reauther")
    monkeypatch.setattr(env.jobs, "fc_save_session", lambda s: saved.append(s))
    assert env.jobs._fc_reauth({"sid": "old"})["sid"] == "NEW"
    assert saved and saved[0]["sid"] == "NEW"


def _fake_reauther(sess):
    return {"sid": "NEW", "csrf": "c"}
