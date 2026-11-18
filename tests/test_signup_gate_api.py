import pytest

from tests.test_api_public import fake_auth, seed


def _signup(frappe, email="n@x.io", **kw):
    fake_auth(frappe)
    frappe.session.user = "Guest"
    from saas_core.api.v1 import auth
    return auth.signup(email=email, password="longpass123", **kw)


def _allow(frappe, *patterns):
    doc = frappe._Doc(doctype="Platform Settings", name=None)
    doc["signup_allow"] = [frappe._dict(pattern=p) for p in patterns]
    frappe._store.docs[("Platform Settings", None)] = doc


def _code(frappe, out, status=403):
    assert frappe.local.response["http_status_code"] == status
    return out["error"]["code"]


def test_default_closed_blocks(frappe):
    out = _signup(frappe)
    assert _code(frappe, out) == "signup_disabled"
    assert not frappe.db.exists("User", "n@x.io")


def test_open_allows(frappe):
    frappe.conf["saas_core_signup_mode"] = "open"
    assert _signup(frappe)["email"] == "n@x.io"


def test_legacy_public_signup_flag_cannot_open_closed_mode(frappe):
    frappe.conf["saas_core_public_signup"] = 1
    assert _code(frappe, _signup(frappe)) == "signup_disabled"
    assert not frappe.db.exists("User", "n@x.io")


def test_legacy_flag_cannot_bypass_invite_or_allowlist(frappe):
    frappe.conf["saas_core_public_signup"] = 1
    frappe.conf["saas_core_signup_mode"] = "invite"
    assert _code(frappe, _signup(frappe)) == "invite_required"
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    assert _code(frappe, _signup(frappe)) == "not_allowlisted"


def test_public_signup_off_still_disabled_even_if_open(frappe):
    frappe.conf["saas_core_signup_mode"] = "open"
    frappe.conf["saas_core_public_signup"] = 0
    assert _code(frappe, _signup(frappe)) == "signup_disabled"


def test_allowlist(frappe):
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    _allow(frappe, "@corp.io", "vip@x.io")
    assert _code(frappe, _signup(frappe, "n@x.io")) == "not_allowlisted"
    assert _signup(frappe, "bob@corp.io")["email"] == "bob@corp.io"
    assert _signup(frappe, "vip@x.io")["email"] == "vip@x.io"


def test_invite_required_without_token(frappe):
    frappe.conf["saas_core_signup_mode"] = "invite"
    assert _code(frappe, _signup(frappe)) == "invite_required"
    assert _code(frappe, _signup(frappe, invite_token="bogus")) == "invite_required"


def test_invite_valid_token_allows(frappe):
    frappe.conf["saas_core_signup_mode"] = "invite"
    _invite(frappe)
    assert _signup(frappe, invite_token="tok123")["email"] == "n@x.io"
    assert frappe.db.get_value("User", "n@x.io", "email_verified") == 1


def _invite(frappe, **kw):
    from saas_core.api.v1.tenants.team import hash_token
    d = dict(name="m1", email="n@x.io", status="invited", invite_token_hash=hash_token("tok123"),
             expires_at="2999-01-01 00:00:00")
    d.update(kw)
    seed(frappe, "Tenant Member", **d)


def test_invite_bound_to_email(frappe):
    frappe.conf["saas_core_signup_mode"] = "invite"
    _invite(frappe)
    assert _code(frappe, _signup(frappe, "attacker@evil.io", invite_token="tok123")) == "invite_required"
    assert not frappe.db.exists("User", "attacker@evil.io")


def test_invite_without_expiry_invalid(frappe):
    frappe.conf["saas_core_signup_mode"] = "invite"
    _invite(frappe, expires_at=None)
    assert _code(frappe, _signup(frappe, invite_token="tok123")) == "invite_required"


def test_invite_consumed_on_signup(frappe):
    frappe.conf["saas_core_signup_mode"] = "invite"
    _invite(frappe)
    assert _signup(frappe, invite_token="tok123")["email"] == "n@x.io"
    assert frappe.db.get_value("Tenant Member", "m1", "status") == "active"
    assert _code(frappe, _signup(frappe, "other@x.io", invite_token="tok123")) == "invite_required"


def test_allowlist_signup_unverified_until_confirmed(frappe):
    from saas_core.config.signup import is_email_verified
    frappe.conf["saas_core_signup_mode"] = "allowlist"
    _allow(frappe, "@corp.io")
    _signup(frappe, "ceo@corp.io")
    assert is_email_verified("ceo@corp.io") is False
    frappe.db.set_value("User", "ceo@corp.io", "email_verified", 1)
    assert is_email_verified("ceo@corp.io") is True


def test_email_verified_not_required_outside_allowlist(frappe):
    from saas_core.config.signup import is_email_verified
    frappe.conf["saas_core_signup_mode"] = "open"
    _signup(frappe)
    assert is_email_verified("n@x.io") is True


def test_signup_conflict_is_generic(frappe):
    frappe.conf["saas_core_signup_mode"] = "open"
    _signup(frappe)
    out = _signup(frappe)
    assert _code(frappe, out, 409) == "conflict"
    assert "exist" not in str(out).lower() and "taken" not in str(out).lower()
