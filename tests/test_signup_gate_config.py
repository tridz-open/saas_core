import json
import pathlib

import pytest

DT = pathlib.Path(__file__).parent.parent / "saas_core" / "saas_core" / "doctype"


def _setup(frappe, mode=None, patterns=()):
    if mode is not None:
        frappe.conf["saas_core_signup_mode"] = mode
    doc = frappe._Doc(doctype="Platform Settings", name=None)
    doc["signup_allow"] = [frappe._dict(pattern=p) for p in patterns]
    frappe._store.docs[("Platform Settings", None)] = doc
    from saas_core.config.signup import is_signup_allowed
    return is_signup_allowed


def test_default_closed(frappe):
    assert _setup(frappe)("a@b.co") == (False, "signup_disabled")


def test_unknown_mode_is_closed(frappe):
    assert _setup(frappe, "bogus")("a@b.co") == (False, "signup_disabled")


def test_open(frappe):
    assert _setup(frappe, "open")("a@b.co") == (True, None)


def test_invite(frappe):
    f = _setup(frappe, "invite")
    assert f("a@b.co") == (False, "invite_required")
    assert f("a@b.co", invite_ok=True) == (True, None)


@pytest.mark.parametrize("email,ok", [
    ("Boss@Corp.com", True), ("x@acme.io", True), ("x@sub.acme.io", False),
    ("x@other.com", False), ("", False)])
def test_allowlist(frappe, email, ok):
    f = _setup(frappe, "allowlist", ["boss@corp.com", "@acme.io"])
    assert f(email) == ((True, None) if ok else (False, "not_allowlisted"))


def test_allowlist_empty(frappe):
    assert _setup(frappe, "allowlist")("a@b.co") == (False, "not_allowlisted")


def test_doctype_json():
    ps = json.loads((DT / "platform_settings" / "platform_settings.json").read_text())
    f = {x["fieldname"]: x for x in ps["fields"]}
    assert f["signup_mode"]["default"] == "closed"
    assert f["signup_mode"]["options"].split("\n") == ["closed", "invite", "allowlist", "open"]
    assert f["signup_allow"]["options"] == "Signup Allow"
    sa = json.loads((DT / "signup_allow" / "signup_allow.json").read_text())
    assert sa["istable"] == 1 and sa["module"] == ps["module"]
    assert {x["fieldname"] for x in sa["fields"]} == {"pattern", "note", "invited_at"}
