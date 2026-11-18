import json
import pytest
import _api_helpers as h


@pytest.fixture
def env(frappe):
    ns = h.install(frappe)
    ns.ev = h.mod("saas_core.events")
    ns.api = h.mod("saas_core.api.v1.events")
    ns.f = frappe
    h.seed(frappe, "mine"); h.seed(frappe, "theirs", owner="bob@x.com")
    yield ns
    h.restore()


def run(env, n=3, **kw):
    out = []
    it = env.api.generate(sleep=lambda s: None, **kw)
    for chunk in it:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


def drain(env, **kw):
    clock = iter(range(0, 1000, 5))
    return [c for c in env.api.generate(sleep=lambda s: None, clock=lambda: next(clock), max_seconds=100, **kw) if c]


def test_publish_and_replay(env):
    env.ev.publish("job.updated", tenant="mine", job="PJ-1", data={"state": "running"})
    env.ev.publish("subscription.updated", tenant="mine")
    out = drain(env, user="alice@x.com", cursor=0)
    frames = [c for c in out if c.startswith("id:")]
    assert len(frames) == 2 and frames[0].startswith("id: 1\nevent: job.updated\n")
    assert json.loads(frames[1].split("data: ")[1])["type"] == "subscription.updated"
    assert any(c == ": hb\n\n" for c in out)


def test_isolation_and_admin(env):
    env.ev.publish("subscription.updated", tenant="theirs"); env.ev.publish("subscription.updated", tenant="mine")
    mine = [c for c in drain(env, user="alice@x.com", cursor=0) if c.startswith("id:")]
    assert len(mine) == 1 and "mine" in mine[0]
    env.f._roles = ["SaaS Admin"]
    assert len([c for c in drain(env, user="root@x.com", cursor=0) if c.startswith("id:")]) == 2


def test_resync_on_gap(env):
    env.ev.publish("subscription.updated", tenant="mine")
    out = drain(env, user="alice@x.com", cursor=99)
    assert out[0].startswith("id: 1\nevent: resync")


def test_stream_authz_and_publish_safe(env):
    r = env.api.stream(tenant="theirs")
    assert r["error"]["code"] == "not_found"
    env.f.publish_realtime = lambda *a, **k: 1 / 0
    env.ev.publish("job.enqueued", tenant="mine", job="J")  # must not raise
    assert env.ev.read_log()["events"][0]["type"] == "job.updated"


def test_on_job_update(env):
    doc = env.f._Doc(doctype="Provision Job", name="PJ-9", tenant="mine", state="running", steps=[{"seq": 2, "name": "x", "state": "running"}])
    env.ev.on_job_update(doc, "on_update")
    assert [e["type"] for e in env.ev.read_log()["events"]] == ["job.updated", "job.step"]
