import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.t = h.mod("saas_core.api.v1.tenants")
    ns.b = h.mod("saas_core.api.v1.tenants.backups")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    yield ns
    h.restore()


def test_create_checkout_required(env):
    h.seed(env.f, "other", owner="bob@x.com"); 
    out = env.t.create(slug="newco", plan="starter")
    h.check_schema("TenantCreated", out)
    assert out["checkout_required"] is True and out["tenant"]["status"] == "pending"
    assert env.f.local.response["http_status_code"] == 202
    assert env.enqueued == []  # not dispatched until payment


def test_create_paying_user_second_tenant_needs_checkout(env):
    h.seed(env.f, "other", owner="bob@x.com"); h.seed(env.f, "acme"); h.sub(env.f)  # alice pays for acme
    out = env.t.create(slug="newco", plan="starter")
    assert out["checkout_required"] is True and out["job"]["state"] == "pending"
    assert env.enqueued == []


def test_create_with_tenant_linked_subscription_enqueues(env):
    h.seed(env.f, "other", owner="bob@x.com"); h.sub(env.f, tenant="newco")  # checkout-created sub for this slug
    out = env.t.create(slug="newco", plan="starter")
    h.check_schema("TenantCreated", out)
    assert "checkout_required" not in out and env.enqueued == [("newco", "provision", None)]
    assert env.f.get_doc("Tenant", "newco").template == "erp"
    assert any(a[0] == "tenant.create" for a in env.audit)


def test_create_admin_override_provisions(env):
    env.f._roles = ["SaaS Admin"]
    h.seed(env.f, "other", owner="bob@x.com")
    out = env.t.create(slug="newco", plan="starter")
    assert "checkout_required" not in out and env.enqueued == [("newco", "provision", None)]


def test_create_hidden_plan_and_template_rejected(env):
    h.seed(env.f, "x1")
    env.f.get_doc({"doctype": "Plan", "name": "secret", "code": "secret", "template": "erp", "is_public": 0, "available": 1}).insert()
    env.f.get_doc({"doctype": "Plan", "name": "gone", "code": "gone", "template": "erp", "is_public": 1, "available": 0}).insert()
    env.f.get_doc({"doctype": "Template", "name": "priv", "is_public": 0}).insert()
    env.f.get_doc({"doctype": "Plan", "name": "big", "code": "big", "is_public": 1, "available": 1, "sort_order": 5}).insert()
    env.f.get_doc({"doctype": "Template", "name": "gated", "is_public": 1, "min_plan": "big"}).insert()
    for kw in ({"plan": "secret"}, {"plan": "gone"}, {"plan": "starter", "template": "priv"}, {"plan": "starter", "template": "gated"}):
        with pytest.raises(env.ApiError) as e: env.t.create(slug="newco", **kw)
        assert e.value.http_status == 422
    assert env.f.db.exists("Tenant", "newco") is None or not env.f.db.exists("Tenant", "newco")
    env.f._roles = ["SaaS Admin"]
    assert env.t.create(slug="newco", plan="secret")["tenant"]["plan"] == "secret"


def test_create_idempotent(env):
    h.seed(env.f, "x1")
    env.f.form_dict["idempotency_key"] = "k1"
    a = env.t.create(slug="newco", plan="starter")
    b = env.t.create(slug="newco", plan="starter")  # replay, no 409
    assert a == b and env.enqueued == []
    with pytest.raises(env.ApiError) as e: env.t.create(slug="other-co", plan="starter")
    assert e.value.code == "idempotency_mismatch" and e.value.http_status == 409


def test_list_get_enumeration_safety(env):
    h.seed(env.f, "mine"); h.seed(env.f, "theirs", owner="bob@x.com")
    out = env.t.list()
    assert [t["slug"] for t in out["items"]] == ["mine"] and out["next_cursor"] is None
    [h.check_schema("Tenant", t) for t in out["items"]]
    h.check_schema("Tenant", env.t.get(tenant="mine"))
    for slug in ("theirs", "ghost"):
        with pytest.raises(env.ApiError) as e: env.t.get(tenant=slug)
        assert e.value.http_status == 404


def test_request_delete(env):
    h.seed(env.f, "acme")
    with pytest.raises(env.ApiError) as e: env.t.request_delete(tenant="acme", confirm_slug="nope")
    assert e.value.http_status == 400
    job = env.t.request_delete(tenant="acme", confirm_slug="acme", reason="bye")
    h.check_schema("Job", job)
    assert job["kind"] == "terminate" and job["state"] == "pending" and env.f.local.response["http_status_code"] == 202
    (apr,) = env.f.get_all("Approval Request")
    assert apr.op == "terminate" and apr.target == "acme" and apr.requested_by == "alice@x.com" and apr.status == "pending"
    assert env.enqueued == [("acme", "terminate", apr.name)]
    assert env.f.get_doc("Tenant", "acme").status == "active"  # nothing happens before approval
    h.seed(env.f, "theirs", owner="bob@x.com")
    with pytest.raises(env.ApiError) as e: env.t.request_delete(tenant="theirs", confirm_slug="theirs")
    assert e.value.http_status == 404


def test_suspend_status(env):
    t = h.seed(env.f, "acme", status="suspended"); t.suspend_reason = "dunning"
    env.f.get_doc({"doctype": "Subscription", "name": "S1", "tenant": "acme", "dunning_stage": 3}).insert()
    out = env.t.suspend_status(tenant="acme")
    h.check_schema("SuspendStatus", out)
    assert out["suspended"] and out["reason"] == "dunning" and out["dunning_stage"] == 3 and out["resume_by_paying"]
    env.f.get_doc("Tenant", "acme").status = "active"
    out = env.t.suspend_status(tenant="acme")
    h.check_schema("SuspendStatus", out); assert not out["suspended"] and out["reason"] is None


def test_login_link(env):
    t = h.seed(env.f, "acme")
    out = env.t.open_login_link(tenant="acme")
    h.check_schema("LoginLink", out)
    assert out["url"].startswith("https://acme.fc.example") and env.port.calls[0][0] == "login_link"
    assert any(a[0] == "tenant.login_link" and "sso" not in str(a) for a in env.audit)
    t.status = "suspended"
    with pytest.raises(env.ApiError) as e: env.t.open_login_link(tenant="acme")
    assert e.value.http_status == 409
    t.status = "active"; env.port.fail = True
    with pytest.raises(env.ApiError) as e: env.t.open_login_link(tenant="acme")
    assert "frappecloud" not in e.value.message and "token" not in e.value.message


def test_login_link_not_owner(env):
    h.seed(env.f, "theirs", owner="bob@x.com")
    with pytest.raises(env.ApiError) as e: env.t.open_login_link(tenant="theirs")
    assert e.value.http_status == 404 and env.port.calls == []


def test_backups(env):
    h.seed(env.f, "acme")
    p1 = env.b.list(tenant="acme", limit=2)
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    [h.check_schema("Backup", b) for b in p1["items"]]
    assert p1["items"][0]["name"] == "bk4"
    p3 = env.b.list(tenant="acme", limit=2, cursor=env.b.list(tenant="acme", limit=2)["next_cursor"])
    p3 = env.b.list(tenant="acme", limit=2, cursor=p3["next_cursor"])
    assert len(p3["items"]) == 1 and p3["next_cursor"] is None
    job = env.b.create(tenant="acme")
    h.check_schema("Job", job); assert job["kind"] == "backup" and env.f.local.response["http_status_code"] == 202
    dl = env.b.download_link(tenant="acme", backup="bk1", part="database")
    h.check_schema("DownloadLink", dl)
    for kw in (dict(backup="bk1", part="weird"), dict(backup="missing", part="public")):
        with pytest.raises(env.ApiError) as e: env.b.download_link(tenant="acme", **kw)
        assert e.value.http_status in (400, 404)
    h.seed(env.f, "theirs", owner="bob@x.com")
    for fn, kw in ((env.b.list, {}), (env.b.create, {}), (env.b.download_link, dict(backup="bk1", part="public"))):
        with pytest.raises(env.ApiError) as e: fn(tenant="theirs", **kw)
        assert e.value.http_status == 404


def test_backup_create_idempotent(env):
    h.seed(env.f, "acme"); env.f.form_dict["idempotency_key"] = "b1"
    assert env.b.create(tenant="acme") == env.b.create(tenant="acme") and len(env.enqueued) == 1


def _avail(env, slug):
    out = env.t.slug_availability(slug=slug)
    h.check_schema("SlugAvailability", out)
    return out


def test_slug_availability_free(env):
    out = _avail(env, "  NewCo ")
    assert out == {"slug": "newco", "available": True, "reason": None, "message": "Available", "suggestion": None}
    assert any(k.startswith("tenants.slug_availability") for k in env.rate)


def test_slug_availability_taken_suggests_next(env):
    h.seed(env.f, "acme"); h.seed(env.f, "acme-2")
    out = _avail(env, "acme")
    assert (out["available"], out["reason"], out["message"]) == (False, "taken", "That address is taken.")
    assert out["suggestion"] == "acme-3"


def test_slug_availability_pending_checkout_reserves(env):
    env.f.get_doc({"doctype": "Checkout Session", "name": "cs1", "tenant": "reservedco"}).insert()
    assert _avail(env, "reservedco")["reason"] == "taken"
    env.f.get_doc({"doctype": "Checkout Session", "name": "cs2", "tenant": "doneco", "consumed_at": "2026-01-01"}).insert()
    assert _avail(env, "doneco")["available"] is True


def test_slug_availability_reserved(env):
    out = _avail(env, "admin")
    assert (out["available"], out["reason"], out["message"]) == (False, "reserved", "That address is reserved.")
    assert out["suggestion"] == "admin-2"
    with pytest.raises(env.ApiError) as e: env.t.create(slug="admin", plan="starter")
    assert e.value.http_status == 422 and e.value.field_errors == {"slug": "reserved"}


def test_slug_availability_invalid_with_suggestion(env):
    out = _avail(env, "1 My Shop!!")
    assert out["reason"] == "invalid" and out["available"] is False
    assert out["message"] == "Use 4–32 lowercase letters, numbers or dashes, starting with a letter."
    assert out["suggestion"] == "x1-my-shop"
    assert _avail(env, "ab")["suggestion"] == "ab-2"
    assert _avail(env, "")["suggestion"] is None


def test_checkout_rejects_hidden_plan_and_template(env):
    from saas_core.api.v1._selection import check_selectable
    env.f.get_doc({"doctype": "Plan", "name": "secret", "code": "secret", "is_public": 0, "available": 1}).insert()
    h.seed(env.f, "x1")
    with pytest.raises(env.ApiError) as e: check_selectable("alice@x.com", "secret", None)
    assert e.value.http_status == 422
    check_selectable("alice@x.com", "starter", "erp")  # public ok
    env.f._roles = ["System Manager"]
    check_selectable("alice@x.com", "secret", None)  # operator ok
