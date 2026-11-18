"""Unattended Frappe Cloud re-login via an ops mailbox (IMAP OTP). Wire with site_config
`saas_core_fc_reauther=saas_core.fc_reauth.imap_reauther`. Signature: fn(session) -> new session | None.

Secrets (the IMAP password, sid, csrf, OTP) are never logged or audited; audit carries the outcome only.
"""
from __future__ import annotations

import importlib
import time

import frappe

LOCK_KEY = "saas_core:fc_reauth"
K_LAST_ATTEMPT = "saas_core_fc_reauth_last"
COOLDOWN_SECONDS = 600
LOCK_TTL = 300
LOCK_WAIT = 200
CONF_KEYS = ("saas_core_fc_email", "saas_core_fc_imap_host", "saas_core_fc_imap_user", "saas_core_fc_imap_password")


def _audit(outcome: str) -> None:
    try:
        importlib.import_module("saas_core.audit").log("fc.reauth", details={"outcome": outcome})
    except Exception:
        pass  # auditing must never break the reauth chain


def _cooling_down() -> bool:
    last = frappe.cache().get_value(K_LAST_ATTEMPT)
    try:
        return bool(last) and (time.time() - float(last)) < COOLDOWN_SECONDS
    except (TypeError, ValueError):
        return False


def _fresher_session(old: dict | None) -> dict | None:
    """Another worker may have re-authed while we waited on the lock; reuse its session."""
    try:
        cur = importlib.import_module("saas_core.jobs").fc_load_session()
    except Exception:
        return None
    if cur and cur.get("sid") and cur.get("sid") != (old or {}).get("sid"):
        return {"sid": cur["sid"], "csrf": cur.get("csrf"), "email": cur.get("email")}
    return None


def imap_reauther(session: dict | None) -> dict | None:
    conf = {k: frappe.conf.get(k) for k in CONF_KEYS}
    if not all(conf.values()):
        _audit("not_configured")
        return None
    audit = importlib.import_module("saas_core.audit")
    try:
        with audit.cache_lock(LOCK_KEY, ttl=LOCK_TTL, wait=LOCK_WAIT):
            if _cooling_down():
                fresh = _fresher_session(session)
                if fresh:
                    _audit("reused_fresh_session")
                    return fresh
                _audit("cooldown")
                return None
            frappe.cache().set_value(K_LAST_ATTEMPT, time.time(), expires_in_sec=COOLDOWN_SECONDS)
            return _login(conf, session)
    except audit.LockTimeout:
        _audit("lock_timeout")
        return None
    except Exception:
        _audit("error")
        return None


def _login(conf: dict, session: dict | None) -> dict | None:
    try:
        from fcctl import auth
        from fcctl._sync.central import CentralClient

        jobs = importlib.import_module("saas_core.jobs")
        store = jobs._MemStore({})
        central = CentralClient(store)
        poller = auth.ImapCodePoller(conf["saas_core_fc_imap_host"], conf["saas_core_fc_imap_user"],
                                     conf["saas_core_fc_imap_password"])
        auth.login_with_code(central, conf["saas_core_fc_email"], poller)
        if not store.sid:
            _audit("failed")
            return None
        _audit("success")
        return {"sid": store.sid, "csrf": store.csrf, "email": conf["saas_core_fc_email"]}
    except Exception:
        _audit("failed")  # outcome only: exception text could embed mailbox details
        return None
