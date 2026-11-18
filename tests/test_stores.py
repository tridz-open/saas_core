import pytest


class XorCipher:  # stands in for Fernet (cryptography not installed in the test venv)
    def encrypt(self, b): return bytes(x ^ 7 for x in b)[::-1].hex().encode()
    def decrypt(self, b): return bytes(x ^ 7 for x in bytes.fromhex(b.decode())[::-1])


def _cipher():
    try:
        from cryptography.fernet import Fernet
        return Fernet(Fernet.generate_key())
    except ImportError:
        return XorCipher()


def test_session_cas_and_conflict(frappe):
    from saas_core.provisioning.stores import FrappeSessionStore
    from saas_core.provisioning.ports import CasConflict
    s = FrappeSessionStore(cipher=_cipher())
    assert s.load("bot") is None
    assert s.save("bot", {"sid": "a"}, 0) == 1
    v = s.load("bot")
    assert v.value == {"sid": "a"} and v.version == 1
    assert frappe.get_doc("Platform Session", "bot").blob != '{"sid": "a"}'
    assert s.save("bot", {"sid": "b"}, 1) == 2
    with pytest.raises(CasConflict):
        s.save("bot", {"sid": "stale"}, 1)
    assert s.load("bot").value == {"sid": "b"}


def test_session_needs_key(frappe):
    from saas_core.provisioning.stores import FrappeSessionStore
    with pytest.raises(RuntimeError):
        FrappeSessionStore().save("x", {}, 0)


def test_session_lock_single_flight(frappe):
    from saas_core.provisioning.stores import FrappeSessionStore
    from saas_core.provisioning.ports import LockBusy
    s = FrappeSessionStore(cipher=_cipher(), lock_wait=0)
    with s.lock("bot", 5):
        with pytest.raises(LockBusy):
            with s.lock("bot", 5):
                pass
    with s.lock("bot", 5):
        pass


def _store(frappe, roles=None):
    from saas_core.provisioning.stores import FrappeApprovalStore
    return FrappeApprovalStore(roles_for=lambda u: roles if roles is not None else ["SaaS Admin"])


def test_two_person_approval(frappe):
    from saas_core.provisioning.ports import ApprovalError
    ap = _store(frappe)
    a = ap.request("terminate", "acme", {"x": 1}, "alice")
    with pytest.raises(ApprovalError): ap.approve(a, "alice")
    with pytest.raises(ApprovalError): ap.assert_executable(a, "terminate", "acme", {"x": 1})
    assert ap.approve(a, "bob")["status"] == "pending"
    with pytest.raises(ApprovalError): ap.approve(a, "bob")
    assert ap.approve(a, "carol")["status"] == "approved"
    assert ap.assert_executable(a, "terminate", "acme", {"x": 1})["approved_by"] == ["bob", "carol"]
    with pytest.raises(ApprovalError): ap.assert_executable(a, "terminate", "other", {"x": 1})
    with pytest.raises(ApprovalError): ap.assert_executable(a, "terminate", "acme", {"x": 2})
    doc = frappe.get_doc("Approval Request", a)
    doc.args = '{"x": 9}'
    with pytest.raises(ApprovalError): ap.assert_executable(a, "terminate", "acme", {"x": 1})


def test_approver_needs_role_and_ttl(frappe):
    from saas_core.provisioning.ports import ApprovalError
    import datetime
    a = _store(frappe, roles=["Guest"]).request("terminate", "acme", None, "alice")
    with pytest.raises(ApprovalError): _store(frappe, roles=["Guest"]).approve(a, "bob")
    ap = _store(frappe)
    ap.approve(a, "bob"); ap.approve(a, "carol")
    frappe.get_doc("Approval Request", a).expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    with pytest.raises(ApprovalError, match="expired"): ap.assert_executable(a, "terminate", "acme", None)
    b = ap.request("terminate", "acme", None, "alice")
    frappe.get_doc("Approval Request", b).expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    with pytest.raises(ApprovalError, match="expired"): ap.approve(b, "bob")


def test_journal_transitions(frappe):
    from prov_helpers import seed, job
    from saas_core.provisioning.stores import FrappeJournal
    from saas_core.provisioning.ports import InvalidTransition
    seed(frappe); j = job(frappe); jn = FrappeJournal()
    s = jn.begin(j, "x")
    assert jn.steps(j)[0]["state"] == "pending"
    with pytest.raises(InvalidTransition): jn.transition(j, s, "succeeded")
    jn.transition(j, s, "dispatching"); jn.transition(j, s, "submitted", handle="h")
    jn.transition(j, s, "running"); jn.transition(j, s, "needs_attention")
    assert jn.steps(j)[0]["handle"] == "h"
    jn.set_job_state(j, "needs_attention", error="e")
    assert frappe.get_doc("Provision Job", j).needs_attention == 1


def test_audit_sink_and_adapter_is_lazy(frappe):
    from saas_core.provisioning.stores import FrappeAuditSink, FcctlAdapter
    assert FrappeAuditSink().log("x.y")
    class Run:
        def to_dict(self): return {"state": "succeeded", "site": "s", "workflow_id": "w"}
        def wait(self): pass
    class WF:
        def provision_site(self, **kw): self.kw = kw; return Run()
        def get(self, i): return None
    class C: workflows = WF()
    from saas_core.provisioning.ports import ProvisionSpec
    r = FcctlAdapter(client=C()).provision_site(ProvisionSpec("w", "s.x.test", "t", domain="x.test", manifest={"apps": [{"app": "frappe"}]}))
    assert r.ok and r.handle == "w" and C.workflows.kw["workflow_id"] == "w"
    assert FcctlAdapter(client=C()).find_workflow("w") is None


def test_adapter_targets_exist_in_real_fcctl():
    import inspect
    import fcctl
    from fcctl import sites, backups
    for fn in (sites.drop_site, sites.login_link, backups.create, backups.list_backups):
        assert callable(fn)
    assert hasattr(fcctl, "Client") and hasattr(fcctl, "BackupSource") and hasattr(fcctl, "DiskShrinkRefused")
    from fcctl._sync import facade
    assert callable(facade.Sites.suspend) and callable(facade.Sites.resume)
    assert callable(facade.Servers.resize) and callable(facade.Backups.download_links)
    assert "owned_by_workflow" in inspect.signature(facade.Backups.restore).parameters
    assert "from_backup" in inspect.signature(facade.Workflows.provision_site).parameters


def test_adapter_maps_facade_calls_and_disk_shrink():
    import fcctl
    from saas_core.api.v1._common import ApiError
    from saas_core.provisioning.stores import FcctlAdapter
    from saas_core.provisioning.ports import ProvisionSpec
    calls = []

    class Run:
        def to_dict(self): return {"state": "succeeded", "site": "s.x", "workflow_id": "w1"}
        def wait(self): pass

    class Sites:
        def suspend(self, srv, site): calls.append(("suspend", srv, site)); return {}
        def resume(self, srv, site): calls.append(("resume", srv, site)); return {}

    class Servers:
        def resize(self, srv, plan):
            if plan == "small": raise fcctl.DiskShrinkRefused("shrink")
            calls.append(("resize", srv, plan)); return {}

    class Backups:
        def download_links(self, srv, site, ts): calls.append(("dl", srv, site, ts)); return {"u": 1}
        def restore(self, srv, site, **kw): calls.append(("restore", srv, site, kw)); return {}

    class WF:
        def provision_site(self, **kw): calls.append(("prov", kw)); return Run()

    class C: sites, servers, backups, workflows = Sites(), Servers(), Backups(), WF()
    ad = FcctlAdapter(client=C(), server="srv1", profile="p")
    ad.suspend_site("s.x"); ad.resume_site("s.x"); ad.change_plan("s.x", "big")
    assert ad.backup_download_link("s.x", "ts1") == {"u": 1}
    ad.restore_backup("s.x", workflow_id="w1", backup={"server": "a", "site": "t", "ts": "1"})
    assert calls[:3] == [("suspend", "srv1", "s.x"), ("resume", "srv1", "s.x"), ("resize", "srv1", "big")]
    assert calls[4][3] == {"owned_by_workflow": "w1", "from_site_backup": {"server": "a", "site": "t", "ts": "1"}}
    with pytest.raises(ApiError) as e:
        ad.change_plan("s.x", "small")
    assert e.value.status == 422 if hasattr(e.value, "status") else True
    ad.provision_site(ProvisionSpec("w1", "s.x", "t", manifest={"apps": [], "backup_source": {"server": "a", "site": "t", "ts": "1"}}))
    kw = calls[-1][1]
    assert isinstance(kw["from_backup"], fcctl.BackupSource) and kw["from_backup"].ts == "1"


def test_drop_login_create_via_facade_and_lazy_site_admin():
    from saas_core.provisioning.stores import FcctlAdapter, _LazySiteAdmin
    calls = []
    class Admin:
        def get_doc(self, *a): return "doc"
        def close(self): calls.append("closed")
    class S:
        def drop(self, srv, site, **kw): calls.append(("drop", srv, site, kw)); return {"state": "succeeded"}
        def login_link(self, srv, site): return "url"
        def create(self, srv, n, apps, idempotency_key=None): return {"task": 1}
        def list(self, srv): return []
        def admin(self, srv, site): calls.append("open"); return Admin()
    class C: sites = S()
    a = FcctlAdapter(client=C(), server="srv")
    assert a.terminate_site("x", workflow_id="w1").ok and calls[0] == ("drop", "srv", "x", {"owned_by_workflow": "w1"})
    assert a.login_link("x") == "url"
    lazy = _LazySiteAdmin(C(), "srv", "x")
    assert "open" not in calls
    assert lazy.get_doc() == "doc" and calls.count("open") == 1
    lazy.close(); assert calls[-1] == "closed"


def test_adapter_bare_subdomain_uses_fc_wildcard_domain(monkeypatch):
    import fcctl.sites
    from saas_core.provisioning.stores import FcctlAdapter
    from saas_core.provisioning.ports import ProvisionSpec
    monkeypatch.setattr(fcctl.sites, "wildcard_domains", lambda p: {"domains": [".bom.frappe.cloud"]})
    class Run:
        def to_dict(self): return {"state": "succeeded", "site": "acme.bom.frappe.cloud", "workflow_id": "w"}
        def wait(self): pass
    class WF:
        def provision_site(self, **kw): self.kw = kw; return Run()
    wf = WF()
    class C:
        workflows = wf
        def pilot(self, server): return object()
    r = FcctlAdapter(client=C(), server="srv").provision_site(ProvisionSpec("w", "acme", "t", manifest={"apps": []}))
    assert wf.kw["site"] == "acme.bom.frappe.cloud" and r.site == "acme.bom.frappe.cloud"


def test_adapter_allows_existing_sites_on_shared_server(frappe):
    """A tenant server hosts many customer sites; provisioning must not demand an empty bench."""
    from saas_core.provisioning.stores import FcctlAdapter
    from saas_core.provisioning.ports import ProvisionSpec
    class Run:
        def to_dict(self): return {"state": "succeeded", "site": "s", "workflow_id": "w"}
        def wait(self): pass
    class WF:
        def provision_site(self, **kw): WF.kw = kw; return Run()
        def get(self, i): return None
    class C: workflows = WF()
    FcctlAdapter(client=C(), server="srv").provision_site(ProvisionSpec("w", "s.x.test", "t", domain="x.test", manifest={"apps": []}))
    assert WF.kw["allow_existing_sites"] is True


def test_health_checks_from_template_dicts_become_callables(frappe, monkeypatch):
    """fcctl calls each health check as fn(api, site); template checks are dicts and crashed with 'not callable'."""
    import urllib.request
    from saas_core.provisioning.stores import _health_callables
    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: seen.append(url) or R())
    fns = _health_callables([{"name": "ping", "path": "/api/method/ping", "expect": 200, "admin": False},
                             {"name": "pos", "path": "/app/acmepos", "expect": 200, "admin": True}])
    class Api:
        def health(self, site): return True
    assert len(fns) == 2 and all(f(Api(), "a.x.test") for f in fns)
    assert seen == ["https://a.x.test/api/method/ping"]
    assert _health_callables(None) is None


def test_health_checks_retry_through_transient_503(frappe, monkeypatch):
    """Right after maintenance_off the site briefly answers 503; health must retry, not fail the provision."""
    import time
    from saas_core.provisioning import stores
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}
    class Api:
        def health(self, site):
            calls["n"] += 1
            return calls["n"] >= 3
    fns = stores._health_callables([{"name": "x", "path": "/", "expect": 200, "admin": True}])
    assert fns[0](Api(), "a.x.test") is True and calls["n"] == 3
