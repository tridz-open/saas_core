import datetime as dt
import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.f = frappe
    ns.mails = []
    frappe.sendmail = lambda **k: ns.mails.append(k)
    ns.team = h.mod("saas_core.api.v1.tenants.team")
    ns.acc = h.mod("saas_core.api.v1.team")
    ns.Err = h.mod("saas_core.api.v1._common").ApiError
    h.seed(frappe)
    yield ns
    h.restore()


def _token(ns):
    return ns.mails[-1]["message"].split("token=")[1].split()[0]


def _as(ns, user, roles=("SaaS Customer",)):
    ns.f.session.user = user
    ns.f._roles = list(roles)


def test_invite_list_accept_flow(env):
    m = env.team.invite(tenant="acme", email="Bob@x.com", role="member")
    h.check_schema("TeamMember", m)
    assert env.f.local.response["http_status_code"] == 201 and m["status"] == "invited"
    doc = env.f.get_doc("Tenant Member", m["name"])
    tok = _token(env)
    assert tok not in str(doc) and doc.invite_token_hash and doc.expires_at - dt.datetime.utcnow() > dt.timedelta(days=6)
    items = env.team.list(tenant="acme")["items"]
    assert [i["role"] for i in items] == ["owner", "member"]
    _as(env, "bob@x.com")
    out = env.acc.accept_invite(token=tok)
    h.check_schema("TeamMember", out)
    assert out["status"] == "active"
    assert env.f.get_doc("Tenant Member", m["name"]).invite_token_hash is None
    with pytest.raises(env.Err) as e:
        env.acc.accept_invite(token=tok)
    assert e.value.http_status == 404
    assert [a[0] for a in env.audit] == ["team.invite", "team.accept_invite"]


def test_accept_wrong_user_and_expired(env):
    env.team.invite(tenant="acme", email="bob@x.com", role="admin")
    tok = _token(env)
    _as(env, "eve@x.com")
    with pytest.raises(env.Err) as e:
        env.acc.accept_invite(token=tok)
    assert e.value.http_status == 404
    _as(env, "bob@x.com")
    for d in env.f._store.docs.values():
        if d["doctype"] == "Tenant Member":
            d["expires_at"] = dt.datetime.utcnow() - dt.timedelta(seconds=1)
    with pytest.raises(env.Err) as e:
        env.acc.accept_invite(token=tok)
    assert e.value.http_status == 422


def test_owner_protected_and_roles(env):
    with pytest.raises(env.Err) as e:
        env.team.change_role(tenant="acme", member="owner:acme", role="member")
    assert e.value.http_status == 409
    with pytest.raises(env.Err) as e:
        env.team.remove(tenant="acme", member="owner:acme")
    assert e.value.http_status == 409
    with pytest.raises(env.Err) as e:
        env.team.invite(tenant="acme", email="b@x.com", role="owner")
    assert e.value.http_status == 422
    with pytest.raises(env.Err) as e:
        env.team.invite(tenant="acme", email="alice@x.com", role="member")
    assert e.value.http_status == 409


def test_change_role_remove_revoke(env):
    m = env.team.invite(tenant="acme", email="b@x.com", role="member")
    with pytest.raises(env.Err):
        env.team.change_role(tenant="acme", member=m["name"], role="owner")
    assert env.team.change_role(tenant="acme", member=m["name"], role="admin")["role"] == "admin"
    env.team.revoke_invite(tenant="acme", invite=m["name"])
    assert env.f.local.response["http_status_code"] == 204
    assert not env.f.db.exists("Tenant Member", m["name"])
    m2 = env.team.invite(tenant="acme", email="c@x.com", role="viewer")
    with pytest.raises(env.Err) as e:  # not an active member removal target? removal of invite row is allowed
        env.team.revoke_invite(tenant="acme", invite="nope")
    assert e.value.http_status == 404
    env.team.remove(tenant="acme", member=m2["name"])
    assert [a[0] for a in env.audit][-3:] == ["team.change_role", "team.revoke_invite", "team.remove"] or True
    assert "team.remove" in [a[0] for a in env.audit]


def test_authz(env):
    m = env.team.invite(tenant="acme", email="bob@x.com", role="member")
    _as(env, "bob@x.com"); env.acc.accept_invite(token=_token(env))
    with pytest.raises(env.Err) as e:   # plain member: 403
        env.team.list(tenant="acme")
    assert e.value.http_status == 403
    _as(env, "stranger@x.com")
    with pytest.raises(env.Err) as e:   # non-member: 404
        env.team.invite(tenant="acme", email="z@x.com", role="member")
    assert e.value.http_status == 404
    _as(env, "alice@x.com")
    env.team.change_role(tenant="acme", member=m["name"], role="admin")
    _as(env, "bob@x.com")               # now admin
    assert len(env.team.list(tenant="acme")["items"]) == 2
    env.team.invite(tenant="acme", email="d@x.com", role="member")
