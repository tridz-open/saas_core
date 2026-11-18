import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _conf(frappe):
    frappe.conf["saas_core_fake_fcctl"] = 1
    frappe.conf["saas_core_session_key"] = Fernet.generate_key().decode()
    frappe.conf["saas_core_alert_email"] = "ops@test"
    frappe.sendmail = lambda **k: frappe._store.messages.append(("mail", k))
    frappe._roles = ["SaaS Admin"]


def _page():
    import importlib
    return importlib.import_module("saas_core.saas_core.page.fc_connect.fc_connect")


def _jobs():
    import importlib
    return importlib.import_module("saas_core.jobs")


def test_send_verify_status_encrypted(frappe):
    p = _page()
    assert p.send_code("a@b.c")["sent"]
    assert not p.status()["connected"]
    assert p.verify("123456", "a@b.c")["connected"]
    st = p.status()
    assert st["connected"] and not st["paused"] and st["whoami"]["capabilities"]
    assert "fake-sid" not in frappe.get_doc("Platform Session", "fc-bot").blob


def test_bad_code_and_role_guard(frappe):
    p = _page()
    with pytest.raises(Exception):
        p.verify("000000", "a@b.c")
    frappe._roles = ["SaaS Customer"]
    with pytest.raises(frappe.PermissionError):
        p.status()


def test_keepalive_pause_and_unpause(frappe):
    p, j = _page(), _jobs()
    p.verify("123456", "a@b.c")
    assert j.keepalive()["ok"]
    j.FakeAuth.expired = True
    try:
        r = j.keepalive()
        assert r["paused"] and p.status()["banner"] == "FC session expired: re-connect"
        assert any(m[0] == "mail" for m in frappe._store.messages)
        frappe._store.enqueued.clear()
        jid = j.enqueue_job("T1", "backup")  # held: pending, not enqueued
        assert not frappe._store.enqueued
        assert frappe.get_doc("Provision Job", jid).state == "pending"
    finally:
        j.FakeAuth.expired = False
    out = p.verify("123456", "a@b.c")  # relogin unpauses
    assert out["requeued"] == 1 and not p.status()["paused"]
    assert frappe._store.enqueued


def test_reauther_chain_avoids_pause(frappe):
    j = _jobs()
    _page().verify("123456", "a@b.c")
    frappe.conf["saas_core_fc_reauther"] = "tests.test_fc_connect._reauth"
    j.FakeAuth.expired = True
    try:
        assert j.keepalive()["reauthed"] and not j.fc_is_paused()
    finally:
        j.FakeAuth.expired = False


def _reauth(sess):
    return {"sid": "new", "csrf": "c", "email": sess["email"]}
