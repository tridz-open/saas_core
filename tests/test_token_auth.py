import datetime as dt
import json
import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe, monkeypatch):
    monkeypatch.setenv("E2_REAL", "1")
    ns = h.install(frappe)
    ns.t = h.mod("saas_core.api.v1.api_tokens")
    ns.auth = h.mod("saas_core.token_auth")
    ns.f = frappe
    ns.headers = {}
    frappe.get_request_header = lambda name, default=None: ns.headers.get(name, default)
    frappe.set_user = lambda u: setattr(frappe.session, "user", u)
    frappe.session.user = "alice@x.com"
    ns.secret = ns.t.create(label="ci")["secret"]
    ns.name = next(k[1] for k in frappe._store.docs if k[0] == "API Token")
    frappe.session.user = "Guest"
    yield ns
    h.restore()


def call(env, header):
    env.headers = {"Authorization": header} if header is not None else {}
    env.f.session.user = "Guest"
    return env.auth.validate()


def rejected(env, header):
    from werkzeug.exceptions import Unauthorized
    with pytest.raises(Unauthorized) as ei:
        call(env, header)
    resp = ei.value.get_response()
    assert resp.status_code == 401
    err = json.loads(resp.get_data(as_text=True))["error"]
    assert err["code"] == "unauthorized" and env.secret not in resp.get_data(as_text=True)
    assert env.f.session.user == "Guest"


def doc(env):
    return env.f._store.docs[("API Token", env.name)]


def test_valid_token_sets_user_and_last_used(env):
    call(env, f"Bearer {env.secret}")
    assert env.f.session.user == "alice@x.com"
    assert doc(env)["last_used"] is not None
    call(env, f"bearer {env.secret}")  # scheme is case-insensitive
    assert env.f.session.user == "alice@x.com"


def test_wrong_token(env):
    rejected(env, "Bearer " + "x" * 43)


def test_revoked_token(env):
    doc(env)["revoked"] = 1
    rejected(env, f"Bearer {env.secret}")


def test_expired_token(env):
    doc(env)["expires_at"] = dt.datetime.utcnow() - dt.timedelta(seconds=1)
    rejected(env, f"Bearer {env.secret}")


@pytest.mark.parametrize("bad", ["Bearer", "Bearer ", "Bearer   "])
def test_malformed_header(env, bad):
    rejected(env, bad)


def test_other_schemes_and_no_header_untouched(env):
    for hdr in (None, "token key:secret", "Basic dXNlcjpwdw=="):
        call(env, hdr)
        assert env.f.session.user == "Guest"


def test_does_not_override_existing_session(env):
    env.headers = {"Authorization": "Bearer oauth-token"}
    env.f.session.user = "carol@x.com"
    env.auth.validate()
    assert env.f.session.user == "carol@x.com"


def test_tenant_scoped_token_limits_tenant_access(env):
    env.f.session.user = "alice@x.com"
    h.seed(env.f, "mine"); h.seed(env.f, "other")
    doc(env)["tenant"] = "mine"
    call(env, f"Bearer {env.secret}")
    common = h.mod("saas_core.api.v1._common")
    assert common.tenant_access("mine")
    with pytest.raises(common.ApiError) as ei:
        common.tenant_access("other")
    assert ei.value.http_status == 404
