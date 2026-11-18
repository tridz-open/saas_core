import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe, monkeypatch):
    monkeypatch.setenv("E2_REAL", "1")
    ns = h.install(frappe)
    ns.t = h.mod("saas_core.api.v1.api_tokens")
    ns.me = h.mod("saas_core.api.v1.me")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    frappe.session.user = "alice@x.com"
    h.seed(frappe, "theirs", owner="bob@x.com")
    yield ns
    h.restore()


def test_create_list_revoke(env):
    r = env.t.create(label="ci")
    h.check_schema("ApiTokenCreated", r)
    assert env.f.local.response["http_status_code"] == 201 and r["secret"]
    stored = env.f._store.docs[("API Token", r["token"]["name"])]
    assert stored["token_hash"] == env.t.hash_secret(r["secret"]) and r["secret"] not in str(dict(stored))
    page = env.t.list()
    assert [x["name"] for x in page["items"]] == [r["token"]["name"]]
    assert "secret" not in page["items"][0]
    h.check_schema("ApiToken", page["items"][0])
    assert env.t.revoke(token=r["token"]["name"]) is None
    assert env.f.local.response["http_status_code"] == 204
    assert env.t.revoke(token=r["token"]["name"]) is None  # idempotent
    assert env.t.list()["items"] == []


def test_owner_only_and_validation(env):
    r = env.t.create(label="mine")
    env.f.session.user = "bob@x.com"
    assert env.t.list()["items"] == []
    assert env.t.revoke(token=r["token"]["name"])["error"]["code"] == "not_found"
    env.f.session.user = "alice@x.com"
    assert env.t.create(label="x", tenant="theirs")["error"]["code"] == "not_found"
    assert env.t.create(label="x" * 101)["error"]["code"] == "validation_error"
    assert env.t.create()["error"]["code"] == "bad_request"
    assert env.t.create(label="x", expires_at="2001-01-01T00:00:00Z")["error"]["code"] == "validation_error"
    env.f.session.user = "Guest"
    with pytest.raises(env.f.PermissionError):
        env.t.list()


def test_me_update_name(env):
    env.f.get_doc({"doctype": "User", "name": "alice@x.com", "email": "alice@x.com", "full_name": "A"}).insert()
    out = env.me.update(full_name=" Alice B ")
    h.check_schema("Me", out)
    assert out["full_name"] == "Alice B"
    assert env.me.update(full_name="")["error"]["code"] == "validation_error"
    assert env.me.update(new_password="abcdefghij1")["error"]["code"] == "validation_error"
    assert env.me.update()["error"]["code"] == "bad_request"


def test_me_email_verified_follows_signup_gate(env):
    """me.email_verified is a bool: the stored flag in allowlist mode, True where the server does not require verification."""
    env.f.get_doc({"doctype": "User", "name": "alice@x.com", "email": "alice@x.com", "full_name": "A"}).insert()
    env.f.conf["saas_core_signup_mode"] = "allowlist"
    out = env.me.update(full_name="Alice")
    h.check_schema("Me", out)
    assert out["email_verified"] is False
    env.f.db.set_value("User", "alice@x.com", "email_verified", 1)
    assert env.me.update(full_name="Alice")["email_verified"] is True
    env.f.db.set_value("User", "alice@x.com", "email_verified", 0)
    env.f.conf["saas_core_signup_mode"] = "open"
    assert env.me.update(full_name="Alice")["email_verified"] is True


def test_to_me_omits_email_verified_unless_given():
    from saas_core.api.v1 import serializers as s
    assert "email_verified" not in s.to_me("a@x.io")
    assert s.to_me("a@x.io", email_verified=0)["email_verified"] is False
