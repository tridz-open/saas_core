"""Event bus. publish() appends to a bounded Redis-backed log (frappe.cache) + realtime pub/sub; the SSE endpoint
(api/v1/events.py) replays/tails the log. Everything here is best-effort and never raises into callers."""
from __future__ import annotations

import datetime as _dt
import json

import frappe

LOG_KEY = "saas_core:events:log"
MAX_EVENTS = 500
REPLAY_WINDOW_SECONDS = 600
EVENT_TYPES = ("job.updated", "job.step", "subscription.updated", "resync")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _load() -> dict:
    try:
        raw = frappe.cache().get_value(LOG_KEY)
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if isinstance(raw, dict) and isinstance(raw.get("events"), list):
            return {"seq": int(raw.get("seq") or 0), "events": raw["events"]}
    except Exception:
        pass
    return {"seq": 0, "events": []}


def read_log() -> dict:
    """{'seq': last sequence number, 'events': [Event dicts, oldest first]} pruned to the replay window."""
    log = _load()
    cutoff = (_now() - _dt.timedelta(seconds=REPLAY_WINDOW_SECONDS)).isoformat()
    log["events"] = [e for e in log["events"] if e.get("at", "") >= cutoff][-MAX_EVENTS:]
    return log


def publish(type: str, *, tenant: str | None = None, job: str | None = None, data: dict | None = None) -> None:
    """Best-effort: never raises into callers. Non-schema `job.*` types are folded into job.updated."""
    try:
        payload = dict(data or {})
        if type not in EVENT_TYPES:
            payload.setdefault("event", type)
            type = "job.updated" if type.startswith("job.") else type
        log = _load()
        seq = log["seq"] + 1
        event = {"seq": seq, "type": type, "at": _now().isoformat()}
        if tenant:
            event["tenant"] = tenant
        if job:
            event["job"] = job
        if payload:
            event["data"] = payload
        events = (log["events"] + [event])[-MAX_EVENTS:]
        frappe.cache().set_value(LOG_KEY, json.dumps({"seq": seq, "events": events}, default=str))
        try:
            frappe.publish_realtime("saas_core_event", event)
        except Exception:
            pass
    except Exception:  # pragma: no cover
        pass


def on_job_update(doc, method=None) -> None:
    """doc_events hook for Provision Job (on_update): publish job state/step changes."""
    try:
        steps = doc.get("steps") or []
        publish("job.updated", tenant=doc.get("tenant"), job=doc.get("name"),
                data={"state": doc.get("state"), "kind": doc.get("kind"), "needs_attention": bool(doc.get("needs_attention"))})
        running = [s for s in steps if s.get("state") == "running"]
        if running:
            publish("job.step", tenant=doc.get("tenant"), job=doc.get("name"),
                    data={"step": running[-1].get("step_name") or running[-1].get("name"), "seq": running[-1].get("seq"), "state": "running"})
    except Exception:  # pragma: no cover
        pass
