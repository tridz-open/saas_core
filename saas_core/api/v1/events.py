"""events.stream: Server-Sent Events of job/tenant/subscription changes (text/event-stream)."""
from __future__ import annotations

import json
import time

import frappe


def _hdr(name):
    try:
        return frappe.get_request_header(name)
    except Exception:  # no bound request (bench execute / tests)
        return None

from saas_core import events as bus
from saas_core.api.v1._common import authenticated, ApiError, require_login, require_experimental

HEARTBEAT_SECONDS = 15
POLL_SECONDS = 1.0
MAX_STREAM_SECONDS = 3600


def _err(status: int, code: str, message: str) -> dict:
    r = getattr(frappe.local, "response", None)
    if r is None:
        r = frappe.local.response = frappe._dict()
    r["http_status_code"] = status
    return {"error": {"code": code, "message": message}}


def _is_admin(user: str) -> bool:
    return user == "Administrator" or bool({"SaaS Admin", "System Manager"} & set(frappe.get_roles(user)))


def _frame(ev: dict) -> str:
    return f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


def _parse_cursor(since, last_event_id):
    for v in (last_event_id, since):
        if v not in (None, ""):
            try:
                return int(v)
            except (TypeError, ValueError):
                raise ApiError("validation_error", "since must be an integer", 400, {"since": "integer"})
    return None


def _visible(ev, tenants, job, admin_all):
    if job and ev.get("job") != job:
        return False
    if tenants is not None and ev.get("tenant") not in tenants:
        return False
    return True


def _my_tenants(user):
    return set(frappe.get_all("Tenant", filters={"owner_user": user}, pluck="name"))


def generate(user, tenant=None, job=None, cursor=None, *, sleep=time.sleep, clock=time.monotonic,
             max_seconds=MAX_STREAM_SECONDS, heartbeat=HEARTBEAT_SECONDS, poll=POLL_SECONDS):
    admin = _is_admin(user)
    scope = {tenant} if tenant else None  # admins see all tenants; customers are filtered per event below
    log = bus.read_log()
    last = log["seq"] if cursor is None else cursor
    if cursor is not None:
        evs = log["events"]
        oldest = evs[0]["seq"] if evs else log["seq"] + 1
        if cursor > log["seq"] or (cursor + 1 < oldest and log["seq"] > cursor):
            yield _frame({"seq": log["seq"], "type": "resync", "at": bus._now().isoformat()})
            last = log["seq"]
    start = last_beat = clock()
    yield ": hb\n\n" if cursor is None else ""
    while True:
        log = bus.read_log()
        tenants = scope if scope is not None else (None if admin else _my_tenants(user))
        for ev in log["events"]:
            if ev["seq"] > last:
                last = ev["seq"]
                if _visible(ev, tenants, job, admin):
                    yield _frame(ev)
                    last_beat = clock()
        now = clock()
        if now - start > max_seconds:
            return
        if now - last_beat >= heartbeat:
            last_beat = now
            yield ": hb\n\n"
        sleep(poll)


@authenticated()
def stream(tenant=None, job=None, since=None, **_):
    require_experimental("sse")
    try:
        user = require_login()
        cursor = _parse_cursor(since, _hdr("Last-Event-ID"))
        if tenant:
            from saas_core.permissions import assert_tenant_access
            assert_tenant_access(tenant, user)
        elif job:
            j = frappe.get_doc("Provision Job", job)
            from saas_core.permissions import assert_tenant_access
            assert_tenant_access(j.tenant, user)
    except ApiError as e:
        return _err(e.http_status, e.code, e.message)
    except frappe.DoesNotExistError:
        return _err(404, "not_found", "Resource not found")
    except frappe.PermissionError:
        return _err(403, "forbidden", "Not allowed")
    from werkzeug.wrappers import Response
    resp = Response(generate(user, tenant, job, cursor), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
