import subprocess
from types import SimpleNamespace

import pytest

KEY = "ab" * 28


class Rec:
    def __init__(self, results=None):
        self.calls, self.results = [], list(results or [])

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.results.pop(0) if self.results else (0, "", "")
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(returncode=r[0], stdout=r[1], stderr=r[2])


@pytest.fixture
def mk(frappe, tmp_path):
    frappe.conf.update({"saas_core_local_db_root_password": "rootpw", "saas_core_local_golden_sql": "/g/db.sql.gz",
                        "saas_core_local_bench_bin": "bench"})

    def make(results=None):
        from saas_core.provisioning.local_bench import LocalBenchPort
        rec = Rec(results)
        return LocalBenchPort(runner=rec, bench_path=str(tmp_path)), rec
    return make


def spec():
    from saas_core.provisioning.ports import ProvisionSpec
    return ProvisionSpec(workflow_id="w", site="acme", tenant="acme")


def test_provision_uses_source_sql_then_migrate(mk):
    p, rec = mk()
    r = p.provision_site(spec())
    assert r.ok and r.site == "acme.localhost" and r.handle
    c0 = rec.calls[0][0]
    assert c0[:3] == ["bench", "new-site", "acme.localhost"] and "--source-sql" in c0 and "/g/db.sql.gz" in c0
    assert c0[c0.index("--db-root-password") + 1] == "rootpw"
    assert rec.calls[-1][0] == ["bench", "--site", "acme.localhost", "migrate"]
    assert rec.calls[0][1]["timeout"] and rec.calls[0][1]["capture_output"]


def test_fallback_to_restore_when_source_sql_unsupported(mk, frappe):
    frappe.conf["saas_core_local_golden_files"] = "/g/pub.tar"
    p, rec = mk([(2, "", "Error: no such option: --source-sql"), (0, "", ""), (0, "", ""), (0, "", "")])
    assert p.provision_site(spec()).ok
    cmds = [c for c, _ in rec.calls]
    assert "--source-sql" not in cmds[1]
    assert cmds[2][:4] == ["bench", "--site", "acme.localhost", "restore"] and "--with-public-files" in cmds[2]
    assert cmds[3][-1] == "migrate"


def test_files_tarball_extracted(mk, frappe):
    frappe.conf["saas_core_local_golden_files"] = "/g/pub.tar"
    p, rec = mk()
    assert p.provision_site(spec()).ok
    assert any(c[0] == "tar" and "/g/pub.tar" in c for c, _ in rec.calls)


def test_failure_redacts_passwords(mk):
    p, rec = mk([(1, "", "boom rootpw")])
    r = p.provision_site(spec())
    assert not r.ok and r.state == "failed" and "rootpw" not in r.message and "***" in r.message


def test_timeout_is_failed(mk):
    p, rec = mk([subprocess.TimeoutExpired("bench", 1)])
    r = p.provision_site(spec())
    assert not r.ok and "timed out" in r.message


def test_admin_password_and_config(mk):
    p, rec = mk()
    p.set_admin_password("a.localhost", "pw1")
    p.set_site_config("a.localhost", {"k": 1, "j": "v"})
    cmds = [c for c, _ in rec.calls]
    assert cmds[0] == ["bench", "--site", "a.localhost", "set-admin-password", "pw1"]
    assert ["bench", "--site", "a.localhost", "set-config", "k", "1"] in cmds
    assert ["bench", "--site", "a.localhost", "set-config", "j", "v"] in cmds


def test_set_admin_password_error_redacted(mk):
    p, rec = mk([(1, "pw1", "")])
    with pytest.raises(Exception) as e:
        p.set_admin_password("a.localhost", "pw1")
    assert "pw1" not in str(e.value)


def test_setup_link(mk, frappe):
    p, rec = mk([(0, f'"{KEY}"\n', "")])
    assert p.create_password_setup_link("a.localhost") == f"http://a.localhost:8004/update-password?key={KEY}"
    assert rec.calls[0][0][-1] == "console"  # runs through the tenant console (saas_core not installed there)
    frappe.conf["saas_core_local_tenant_port"] = 8123
    p, _ = mk([(0, KEY, "")])
    assert ":8123/" in p.create_password_setup_link("a.localhost")


def test_mint_reset_key_stores_sha256(frappe, monkeypatch):
    import hashlib, sys, types
    from saas_core.provisioning import local_bench
    sets = []
    frappe.db.set_value = lambda *a, **k: sets.append(a)
    frappe.db.commit = lambda: None
    utils = types.ModuleType("frappe.utils"); utils.now_datetime = lambda: "NOW"
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    key = local_bench._mint_reset_key("Administrator")
    assert len(key) == 56
    dt, user, vals = sets[0]
    assert vals["reset_password_key"] == hashlib.sha256(key.encode()).hexdigest()
    assert vals["last_reset_password_key_generated_on"] == "NOW"


def test_misc(mk):
    p, rec = mk()
    assert p.find_workflow("w") is None
    assert p.login_link("a.localhost") == "http://a.localhost:8004/login"
    assert not hasattr(p, "trust_tier")
    assert p.terminate_site("a.localhost").ok
    c = rec.calls[0][0]
    assert c[:3] == ["bench", "drop-site", "a.localhost"] and "--force" in c
    with pytest.raises(NotImplementedError):
        p.backup_download_link("a", "b")


def test_get_port_selects_local_before_fake(frappe):
    from saas_core.provisioning import runner
    from saas_core.provisioning.local_bench import LocalBenchPort
    runner.set_port(None)
    frappe.conf["saas_core_local_bench"] = 1
    frappe.conf["saas_core_fake_fcctl"] = 1
    assert isinstance(runner.get_port(), LocalBenchPort)
    runner.set_port(None)
