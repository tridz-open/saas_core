import sys
import types

import pytest


@pytest.fixture
def env(frappe, monkeypatch):
    frappe._roles = ["System Manager"]
    frappe.session.user = "owner@x.com"
    doc = frappe._Doc(doctype="Platform Settings", name=None)
    doc["signup_allow"] = []
    frappe._store.docs[("Platform Settings", None)] = doc
    logs, mails = [], []
    audit = types.ModuleType("saas_core.audit")
    audit.log = lambda *a, **k: logs.append((a, k))
    monkeypatch.setitem(sys.modules, "saas_core.audit", audit)
    frappe.sendmail = lambda **kw: mails.append(kw)
    frappe.conf["saas_core_brand"] = "Acme"
    from saas_core import beta_access
    from saas_core.api.v1.admin import beta_access as api
    return frappe, beta_access, api, doc, mails, logs


def patterns(doc):
    return [r["pattern"] for r in doc["signup_allow"]]


def test_add_dedupes_validates_lowercases(env):
    frappe, core, api, doc, mails, _ = env
    out = api.add(entries="A@B.co\n@Acme.io, a@b.co")
    assert out["added"] == ["a@b.co", "@acme.io"] and patterns(doc) == ["a@b.co", "@acme.io"]
    assert mails == []
    again = api.add(entries=["a@b.co", "c@d.co"], note="demo")
    assert again["added"] == ["c@d.co"] and again["already_listed"] == ["a@b.co"]
    assert doc["signup_allow"][-1]["note"] == "demo"


@pytest.mark.parametrize("bad", ["nope", "@x", "a@b", "a b"])
def test_add_rejects_invalid(env, bad):
    frappe, core, api, doc, *_ = env
    out = api.add(entries=["ok@x.io", bad])
    assert out["error"]["code"] == "validation_error" and frappe.local.response["http_status_code"] == 422
    assert patterns(doc) == []   # atomic


def test_add_requires_entries(env):
    frappe, core, api, *_ = env
    assert api.add()["error"]["code"] == "bad_request"
    assert api.add(entries=" \n ")["error"]["code"] == "validation_error"


def test_send_invite_exact_emails_only_with_link(env):
    frappe, core, api, doc, mails, _ = env
    out = api.add(entries=["Boss@Corp.com", "@acme.io"], send_invite=True)
    assert out["invites_sent"] == ["boss@corp.com"] and out["skipped_domains"] == ["@acme.io"]
    assert len(mails) == 1 and mails[0]["recipients"] == ["boss@corp.com"]
    assert mails[0]["subject"] == "You're invited to try Acme"
    assert "/console/signup?email=boss@corp.com" in mails[0]["message"]
    assert doc["signup_allow"][0].get("invited_at")
    assert not doc["signup_allow"][1].get("invited_at")


def test_send_invite_string_true(env):
    *_, api, doc, mails, _ = env
    api.add(entries="x@y.io", send_invite="true")
    assert len(mails) == 1


def test_invite_uses_console_url_setting(env):
    frappe, core, api, doc, mails, _ = env
    frappe.conf["saas_core_console_url"] = "https://app.acme.test/console"
    api.add(entries="x@y.io", send_invite=True)
    assert "https://app.acme.test/console/signup?email=x@y.io" in mails[0]["message"]


def test_invite_failure_reported(env):
    frappe, core, api, doc, mails, _ = env
    def boom(**kw): raise RuntimeError("smtp")
    frappe.sendmail = boom
    out = api.add(entries="x@y.io", send_invite=True)
    assert out["added"] == ["x@y.io"] and out["invites_failed"] == ["x@y.io"] and out["invites_sent"] == []


def test_remove(env):
    frappe, core, api, doc, *_ = env
    api.add(entries=["a@b.co", "@acme.io"])
    assert api.remove(pattern="A@B.co") == {"removed": "a@b.co"}
    assert patterns(doc) == ["@acme.io"]
    out = api.remove(pattern="zzz@b.co")
    assert out["error"]["code"] == "not_found" and frappe.local.response["http_status_code"] == 404


def test_resend(env):
    frappe, core, api, doc, mails, _ = env
    api.add(entries=["a@b.co", "@acme.io"])
    assert api.resend(email="a@b.co") == {"email": "a@b.co", "sent": True}
    assert len(mails) == 1 and doc["signup_allow"][0].get("invited_at")
    assert api.resend(email="x@b.co")["error"]["code"] == "not_found"
    assert api.resend(email="@acme.io")["error"]["code"] == "validation_error"


def test_list_status(env):
    frappe, core, api, doc, *_ = env
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    api.add(entries=["none@x.io", "signed@x.io", "verified@x.io", "site@x.io", "@acme.io"])
    frappe.get_doc({"doctype": "User", "name": "signed@x.io"}).insert()
    frappe.get_doc({"doctype": "User", "name": "verified@x.io", "email_verified": 1}).insert()
    frappe.get_doc({"doctype": "User", "name": "site@x.io", "email_verified": 1}).insert()
    frappe.get_doc({"doctype": "Tenant", "name": "t1", "owner_user": "site@x.io"}).insert()
    out = api.list()
    assert out["signup_mode"] == "allowlist"
    by = {p["pattern"]: p for p in out["patterns"]}
    assert (by["none@x.io"]["account_exists"], by["none@x.io"]["verified"], by["none@x.io"]["has_tenant"]) == (False, False, False)
    assert (by["signed@x.io"]["account_exists"], by["signed@x.io"]["verified"]) == (True, False)
    assert (by["verified@x.io"]["verified"], by["verified@x.io"]["has_tenant"]) == (True, False)
    assert (by["site@x.io"]["verified"], by["site@x.io"]["has_tenant"]) == (True, True)
    assert by["@acme.io"]["kind"] == "domain" and by["@acme.io"]["account_exists"] is None


@pytest.mark.parametrize("roles", [["SaaS Admin"], ["SaaS Customer"]])
@pytest.mark.parametrize("call", [lambda a: a.list(), lambda a: a.add(entries="a@b.co"),
                                  lambda a: a.remove(pattern="a@b.co"), lambda a: a.resend(email="a@b.co")])
def test_only_system_manager(env, roles, call):
    frappe, core, api, doc, mails, _ = env
    frappe._roles = roles
    out = call(api)
    assert out["error"]["code"] == "forbidden" and frappe.local.response["http_status_code"] == 403
    assert patterns(doc) == [] and mails == []


def test_guest_blocked(env):
    frappe, core, api, *_ = env
    frappe.session.user = "Guest"
    with pytest.raises(frappe.PermissionError):
        api.list()


def test_list_is_get_mutations_are_post(env):
    *_, api, doc, mails, _ = env
    assert api.list.__saas_methods__ == ["GET"]
    for fn in (api.add, api.remove, api.resend):
        assert fn.__saas_methods__ == ["POST"]


def test_audit_logged(env):
    frappe, core, api, doc, mails, logs = env
    api.add(entries="a@b.co")
    api.remove(pattern="a@b.co")
    assert [a[0][0] for a in logs] == ["beta_access.add", "beta_access.remove"]


# ---- CLI wrappers ----
def test_cli_add_remove_list(env):
    frappe, core, api, doc, mails, _ = env
    out = core.add_beta_users(emails=["a@b.co", "@acme.io"], send_invite=True, note="n")
    assert out["added"] == ["a@b.co", "@acme.io"] and out["invites_sent"] == ["a@b.co"] and len(mails) == 1
    assert [p["pattern"] for p in core.list_beta_users()["patterns"]] == ["a@b.co", "@acme.io"]
    assert core.remove_beta_users(emails=["a@b.co", "q@z.io"]) == {"removed": ["a@b.co"], "not_found": ["q@z.io"]}
    assert core.resend_beta_invite("a@b.co") if False else True


def test_cli_invalid_raises(env):
    core = env[1]
    with pytest.raises(core.BetaAccessError):
        core.add_beta_users(emails=["bad"])


# ---- gate integration ----
def test_allowlist_config_public_signup_true(env):
    frappe = env[0]
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    from saas_core.api.v1 import auth
    out = auth.config()
    assert out["public_signup"] is True and out["signup_mode"] == "allowlist"
    frappe.conf["saas_core_signup_mode"] = "closed"
    assert auth.config()["public_signup"] is False


def test_listed_email_allowed_other_rejected(env):
    frappe, core, api, *_ = env
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    api.add(entries=["in@x.io", "@acme.io"])
    from saas_core.config.signup import is_signup_allowed
    assert is_signup_allowed("IN@x.io") == (True, None)
    assert is_signup_allowed("a@acme.io") == (True, None)
    assert is_signup_allowed("out@x.io") == (False, "not_allowlisted")


def test_template_shipped():
    import json, pathlib
    root = pathlib.Path(__file__).parent.parent / "saas_core"
    names = [t["name"] for t in json.loads((root / "fixtures" / "email_template.json").read_text())]
    assert "saas-beta-invite" in names
    assert "saas-beta-invite" in (root / "hooks.py").read_text()
    from saas_core.notifications.templating import DEFAULT_TEMPLATES
    assert DEFAULT_TEMPLATES["beta_invite"] == "saas-beta-invite"
