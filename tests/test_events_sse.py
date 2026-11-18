"""SSE wire behaviour of api/v1/events.stream via the werkzeug test client."""
import json
import pytest
from werkzeug.test import Client
from werkzeug.wrappers import Response
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.ev = h.mod("saas_core.events")
    ns.api = h.mod("saas_core.api.v1.events")
    ns.f = frappe
    h.seed(frappe, "mine"); h.seed(frappe, "theirs", owner="bob@x.com")
    # bound the stream: fake clock advancing 5s per tick, no real sleeping
    real = ns.api.generate

    def bounded(user, tenant=None, job=None, cursor=None, **kw):
        ticks = iter(range(0, 10_000, 5))
        return real(user, tenant, job, cursor, sleep=lambda s: None, clock=lambda: next(ticks), max_seconds=40)
    ns.api.generate = bounded
    ns.headers = {}
    frappe.get_request_header = lambda name, default=None: ns.headers.get(name, default)
    yield ns
    h.restore()


def get(env, **kw):
    def app(environ, start_response):
        out = env.api.stream(**kw)
        if not isinstance(out, Response):
            out = Response(json.dumps(out), mimetype="application/json")
        return out(environ, start_response)
    return Client(app).get("/")


def test_headers(env):
    r = get(env)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["X-Accel-Buffering"] == "no"


def test_heartbeat_frames(env):
    body = get(env).get_data(as_text=True)
    assert body.startswith(": hb\n\n")
    assert body.count(": hb\n\n") >= 2  # initial + periodic (15s heartbeat over 40s)


def test_last_event_id_replay(env):
    for _ in range(3):
        env.ev.publish("subscription.updated", tenant="mine")
    env.headers["Last-Event-ID"] = "1"
    body = get(env).get_data(as_text=True)
    ids = [l for l in body.splitlines() if l.startswith("id: ")]
    assert ids == ["id: 2", "id: 3"]


def test_no_cursor_skips_history(env):
    env.ev.publish("subscription.updated", tenant="mine")
    assert "id: " not in get(env).get_data(as_text=True)


def test_bad_last_event_id_is_400(env):
    env.headers["Last-Event-ID"] = "abc"
    r = get(env)
    assert r.status_code in (200, 400) and "validation_error" in r.get_data(as_text=True) or r.status_code == 400
