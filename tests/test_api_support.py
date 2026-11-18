import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.s = h.mod("saas_core.api.v1.support.tickets")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    h.seed(frappe, "mine"); h.seed(frappe, "theirs", owner="bob@x.com")
    yield ns
    h.restore()


def test_create_get_list_reply(env):
    t = env.s.create(subject="Help", body="broken", tenant="mine", priority="high")
    h.check_schema("SupportTicket", t)
    assert t["status"] == "open" and t["priority"] == "high" and env.f.local.response["http_status_code"] == 201
    assert env.s.get(ticket=t["name"])["subject"] == "Help"
    r = env.s.reply(ticket=t["name"], body="more info")
    h.check_schema("SupportTicket", r)
    assert r["replies"][0]["body"] == "more info" and r["replies"][0]["from_staff"] is False
    page = env.s.list(); assert [x["name"] for x in page["items"]] == [t["name"]]
    h.check_schema("SupportTicket", page["items"][0])


def test_validation(env):
    for kw in (dict(subject="", body="x"), dict(subject="s", body=""), dict(subject="s" * 201, body="x"), dict(subject="s", body="b", priority="urgent")):
        with pytest.raises(env.ApiError) as e: env.s.create(**kw)
        assert e.value.http_status == 400 and e.value.field_errors


def test_other_users_ticket_and_tenant_404(env):
    env.f.session.user = "bob@x.com"
    other = env.s.create(subject="mine", body="b")
    env.f.session.user = "alice@x.com"
    assert env.s.list()["items"] == []
    for call in (lambda: env.s.get(ticket=other["name"]), lambda: env.s.reply(ticket=other["name"], body="x"),
                 lambda: env.s.get(ticket="TKT-NOPE"), lambda: env.s.create(subject="s", body="b", tenant="theirs")):
        with pytest.raises(env.ApiError) as e: call()
        assert e.value.http_status == 404


def test_create_idempotent_and_closed_reply(env):
    env.f.form_dict["idempotency_key"] = "t1"
    a = env.s.create(subject="Help", body="b"); b = env.s.create(subject="Help", body="b")
    assert a == b and len(env.f.get_all("Support Ticket")) == 1
    with pytest.raises(env.ApiError) as e: env.s.create(subject="Different", body="b")
    assert e.value.code == "idempotency_mismatch"
    env.f.get_doc("Support Ticket", a["name"]).status = "closed"
    with pytest.raises(env.ApiError): env.s.reply(ticket=a["name"], body="x")
