"""tenants.setup_complete: guest endpoint, HMAC-verified with the per-tenant secret, idempotent."""
import datetime as dt
import hashlib
import hmac
import json
import types

import pytest
import _api_helpers as h

SECRET = "tenant-secret-1"
SITE = "acme.fc.example"


def sign(body: bytes, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def body_for(site=SITE, completed_at=None):
    completed_at = completed_at or dt.datetime.now().replace(microsecond=0).isoformat()
    return json.dumps({"site": site, "completed_at": completed_at}, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def env(frappe):
    ns = h.install(frappe, user="Guest", roles=("Guest",))
    ns.t = h.mod("saas_core.api.v1.tenants")
    ns.ApiError = h.mod("saas_core.api.v1._common").ApiError
    ns.f = frappe
    t = h.seed(frappe, "acme", owner="o@x.com")
    t.get_password = lambda f, raise_exception=True: SECRET if f == "setup_secret" else None
    frappe.utils = types.SimpleNamespace(now_datetime=lambda: dt.datetime(2026, 11, 17, 10, 0, 0))

    def call(body, header="auto", name="X-Saas-Signature", extra=None):
        hdr = {} if header is None else {name: sign(body) if header == "auto" else header}
        hdr.update(extra or {})
        frappe.request = types.SimpleNamespace(get_data=lambda: body, headers=hdr)
        return ns.t.setup_complete()
    ns.call = call
    ns.tenant = t
    yield ns
    h.restore()


def welcomes(env):
    return [kw for m, kw in env.f._store.enqueued if m == "saas_core.notifications.welcome.send_tenant_welcome"]


def test_valid_signature_marks_completed_and_enqueues_welcome(env):
    out = env.call(body_for())
    assert out == {"tenant": "acme", "status": "completed"}
    assert env.tenant["setup_completed_at"] == dt.datetime(2026, 11, 17, 10, 0, 0)
    assert welcomes(env) == [{"tenant": "acme", "enqueue_after_commit": True}]
    assert any(a[0] == "tenant.setup_completed" for a in env.audit)
    assert env.f.generate_hash  # sanity: stub intact


def test_idempotent_replay_does_not_restamp(env):
    env.call(body_for())
    first = env.tenant["setup_completed_at"]
    env.f.utils.now_datetime = lambda: dt.datetime(2026, 11, 18)
    assert env.call(body_for())["status"] == "already_completed"
    assert env.tenant["setup_completed_at"] == first
    assert len(welcomes(env)) == 2        # re-enqueued (lost job recovery); send_tenant_welcome is a no-op once sent


def test_no_enqueue_once_welcome_sent(env):
    env.call(body_for())
    env.tenant["welcome_sent_at"] = dt.datetime(2026, 11, 17, 10, 1)
    env.call(body_for())
    assert len(welcomes(env)) == 1


@pytest.mark.parametrize("case", ["bad_mac", "no_header", "wrong_secret", "unknown_site", "not_json", "stale", "future", "no_secret", "bad_date"])
def test_rejections_are_uniform_401(env, case):
    body = body_for()
    header = "auto"
    if case == "bad_mac":
        header = "sha256=" + "0" * 64
    elif case == "no_header":
        header = None
    elif case == "wrong_secret":
        header = sign(body, "other")
    elif case == "unknown_site":
        body = body_for("nope.fc.example")
    elif case == "not_json":
        body = b"{nope"
    elif case == "stale":
        body = body_for(completed_at=(dt.datetime.now() - dt.timedelta(days=8)).isoformat())
    elif case == "future":
        body = body_for(completed_at=(dt.datetime.now() + dt.timedelta(days=3)).isoformat())
    elif case == "bad_date":
        body = body_for(completed_at="yesterday")
    elif case == "no_secret":
        env.tenant.get_password = lambda f, raise_exception=True: None
    with pytest.raises(env.ApiError) as e:
        env.call(body, header)
    assert e.value.http_status == 401 and e.value.code == "invalid_signature"
    assert not env.tenant.get("setup_completed_at") and not welcomes(env)


def test_signature_covers_the_exact_body(env):
    body = body_for()
    sig = sign(body)
    tampered = body.replace(b"acme", b"acmf")
    with pytest.raises(env.ApiError):
        env.call(tampered, sig)


def test_rate_limited_per_ip(env):
    env.call(body_for())
    assert any(k.startswith("tenants.setup_complete:203.0.113.9") for k in env.rate)


def test_endpoint_is_guest_post_only():
    import ast, pathlib
    src = (pathlib.Path(__file__).parent.parent / "saas_core/api/v1/tenants/__init__.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "setup_complete")
    dec = fn.decorator_list[0]
    kws = {k.arg: getattr(k.value, "value", None) or [e.value for e in k.value.elts] for k in dec.keywords}
    assert kws == {"allow_guest": True, "methods": ["POST"]}


def test_fake_port_set_site_config(frappe):
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    p = FakeFcctl()
    p.set_site_config("a.fc", {"k": "v", "s": "secret-value"})
    p.set_site_config("a.fc", {"k2": 1})
    assert p.sites["a.fc"]["site_config"] == {"k": "v", "s": "secret-value", "k2": 1}
    assert p.count("set_site_config") == 2 and "secret-value" not in repr(p.calls)


def test_legacy_header_rejected_unless_aliased(env):
    with pytest.raises(env.ApiError):
        env.call(body_for(), "auto", name="X-Legacy-Signature")


def test_legacy_header_still_verifies(env):
    env.f.conf["saas_core_setup_signature_header_aliases"] = "X-Legacy-Signature"
    assert env.call(body_for(), "auto", name="X-Legacy-Signature")["status"] == "completed"


def test_new_header_preferred_over_legacy(env):
    env.f.conf["saas_core_setup_signature_header_aliases"] = "X-Legacy-Signature"
    body = body_for()
    # valid new header wins even if the legacy one is garbage
    assert env.call(body, "auto", extra={"X-Legacy-Signature": "sha256=" + "0" * 64})["status"] == "completed"


def test_bad_new_header_not_rescued_by_legacy(env):
    env.f.conf["saas_core_setup_signature_header_aliases"] = "X-Legacy-Signature"
    body = body_for()
    with pytest.raises(env.ApiError) as e:
        env.call(body, "sha256=" + "0" * 64, extra={"X-Legacy-Signature": sign(body)})
    assert e.value.http_status == 401 and e.value.code == "invalid_signature"
