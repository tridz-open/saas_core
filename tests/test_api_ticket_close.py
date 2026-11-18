import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.s = h.mod("saas_core.api.v1.support.tickets")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    h.seed(frappe, "mine")
    yield ns
    h.restore()


def test_close_owner(env):
    t = env.s.create(subject="Help", body="b", tenant="mine")
    r = env.s.close(ticket=t["name"])
    h.check_schema("SupportTicket", r)
    assert r["status"] == "closed"
    assert env.s.close(ticket=t["name"])["status"] == "closed"
    with pytest.raises(env.ApiError): env.s.reply(ticket=t["name"], body="x")


def test_close_not_owner_or_missing(env):
    env.f.session.user = "bob@x.com"
    other = env.s.create(subject="s", body="b")
    env.f.session.user = "alice@x.com"
    for tk in (other["name"], "TKT-NOPE"):
        with pytest.raises(env.ApiError) as e: env.s.close(ticket=tk)
        assert e.value.http_status == 404
    with pytest.raises(env.ApiError) as e: env.s.close()
    assert e.value.http_status == 400
