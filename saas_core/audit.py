"""Hash-chained audit log. Each Audit Event hash = sha256(prev_hash + canonical json of the event)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import time
import uuid

import frappe

GENESIS = "0" * 64


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def cache_lock(key: str, ttl: float = 30, wait: float = 5):
    """Mutex via frappe.cache setnx + TTL. Raises LockTimeout after `wait` seconds."""
    cache = frappe.cache()
    # Raw keys bypass Frappe's per-site prefix, so add the site name ourselves (multi-site benches).
    key = f"{getattr(getattr(frappe, 'local', None), 'site', None) or ''}:{key}"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    # Use RAW redis ops for acquire AND release, with an atomic SET NX EX.
    # (Bug fixed after the real-bench run: acquire used raw `setnx` but release used
    # `get_value`/`delete_value`, which add Frappe's site key prefix, so locks were never
    # released and wedged retries until the TTL expired.)
    raw_set = getattr(cache, "set", None)
    while True:
        if raw_set is not None and hasattr(cache, "connection_pool"):
            acquired = bool(raw_set(key, token, nx=True, ex=max(1, int(ttl))))
        else:  # test stub
            acquired = bool(cache.setnx(key, token))
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise LockTimeout(key)
        time.sleep(0.02)
    try:
        yield
    finally:
        if hasattr(cache, "connection_pool"):
            cur = cache.get(key)
            if cur is not None and (cur.decode() if isinstance(cur, bytes) else cur) == token:
                cache.delete(key)
        elif cache.get_value(key) == token:
            cache.delete_value(key)


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canon(payload)).encode()).hexdigest()


def _payload(seq, at, actor, action, target_doctype, target, details) -> dict:
    return {"seq": seq, "at": str(at), "actor": actor, "action": action, "target_doctype": target_doctype,
            "target": target, "details": details or {}}


def _details(v):
    if isinstance(v, str):
        return json.loads(v) if v else {}
    return v or {}


def log(action: str, *, actor: str | None = None, target_doctype: str | None = None, target: str | None = None,
        details: dict | None = None) -> str:
    """Append an Audit Event; returns its name."""
    from frappe.utils import now_datetime
    with cache_lock("saas_core:audit:seq"):
        last = frappe.get_all("Audit Event", fields=["seq", "hash"], order_by="seq desc", limit=1)
        seq = (last[0].get("seq") or 0) + 1 if last else 1
        prev = last[0].get("hash") if last else GENESIS
        at = now_datetime()
        actor = actor or getattr(frappe.session, "user", None)
        payload = _payload(seq, at, actor, action, target_doctype, target, details)
        doc = frappe.get_doc({"doctype": "Audit Event", "seq": seq, "at": at, "actor": actor, "action": action,
                              "target_doctype": target_doctype, "target": target,
                              "details": json.dumps(details or {}, sort_keys=True, default=str),
                              "prev_hash": prev, "hash": compute_hash(prev, payload)})
        doc.insert(ignore_permissions=True)
        return doc.name


def verify_chain() -> dict:
    """Walk the chain. Returns {"ok", "checked", "broken_at"} (broken_at = first bad seq)."""
    rows = frappe.get_all("Audit Event", fields=["seq", "at", "actor", "action", "target_doctype", "target",
                                                  "details", "prev_hash", "hash"], order_by="seq asc")
    prev, expected_seq, checked = GENESIS, 1, 0
    for r in rows:
        payload = _payload(r.get("seq"), r.get("at"), r.get("actor"), r.get("action"), r.get("target_doctype"),
                           r.get("target"), _details(r.get("details")))
        if (r.get("seq") != expected_seq or r.get("prev_hash") != prev
                or r.get("hash") != compute_hash(prev, payload)):
            return {"ok": False, "checked": checked, "broken_at": r.get("seq")}
        prev, expected_seq, checked = r.get("hash"), expected_seq + 1, checked + 1
    return {"ok": True, "checked": checked, "broken_at": None}
