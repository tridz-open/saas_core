"""Provision Job creation + enqueue with dedupe."""
from __future__ import annotations

import json

import frappe

KINDS = ("provision", "suspend", "resume", "change_plan", "terminate", "backup", "restore")
NON_TERMINAL = ("pending", "dispatching", "submitted", "running", "stuck", "outcome_unknown", "needs_attention")
RUN_JOB = "saas_core.provisioning.runner.run_job"


def enqueue_job(tenant: str, kind: str, *, args: dict | None = None, approval: str | None = None,
                dedupe_key: str | None = None) -> str:
    """Create a Provision Job (pending) and enqueue the runner. Returns the job name.
    If dedupe_key matches a non-terminal job for the tenant, that job is returned and nothing is enqueued."""
    import importlib
    audit = importlib.import_module("saas_core.audit")
    events = importlib.import_module("saas_core.events")
    if kind not in KINDS:
        frappe.throw(f"Unknown job kind {kind}")
    if dedupe_key:
        for j in frappe.get_all("Provision Job", filters={"dedupe_key": dedupe_key},
                                fields=["name", "state", "tenant"]):
            if j.get("tenant") == tenant and j.get("state") in NON_TERMINAL:
                return j["name"]
    doc = frappe.get_doc({"doctype": "Provision Job", "tenant": tenant, "kind": kind, "state": "pending",
                          "attempts": 0, "args": json.dumps(args or {}, default=str), "approval": approval,
                          "dedupe_key": dedupe_key})
    doc.insert(ignore_permissions=True)
    try:
        frappe.db.set_value("Tenant", tenant, "active_job", doc.name)
    except Exception:
        pass
    audit.log("job.enqueued", target_doctype="Provision Job", target=doc.name,
              details={"tenant": tenant, "kind": kind, "approval": approval})
    events.publish("job.enqueued", tenant=tenant, job=doc.name, data={"kind": kind})
    if not fc_is_paused():  # queue paused (FC session lost): job stays pending, not failed
        frappe.enqueue(RUN_JOB, queue="long", timeout=3600, enqueue_after_commit=True, job=doc.name)
    return doc.name


# ======================================================================= FC control-plane session (C4)
# Session blob (Fernet, FrappeSessionStore / Platform Session): {sid, csrf, email, saved_at}.
# The fake backend is used when site_config saas_core_fake_fcctl is set, so no real login ever happens in tests.
import time  # noqa: E402

FC_LOGIN = "fc-bot"
K_PAUSED, K_BANNER, K_LAST_KA, K_LAST_ERR = (f"saas_core:fc:{n}" for n in ("paused", "banner", "last_keepalive", "last_error"))


class FcAuthError(Exception):
    pass


class FakeAuth:
    """Deterministic in-process auth. Code '123456' is valid; FakeAuth.expired = True simulates session loss."""
    expired = False
    sent: list = []

    def send_code(self, email):
        FakeAuth.sent.append(email)
        return {"sent": True}

    def verify_code(self, email, code):
        if str(code) != "123456":
            raise FcAuthError("invalid code")
        return {"sid": "fake-sid-" + email, "csrf": "fake-csrf"}

    def keepalive(self, session):
        if FakeAuth.expired:
            raise FcAuthError("session expired")
        return {"user": session.get("email")}

    def whoami(self, session):
        if FakeAuth.expired:
            raise FcAuthError("session expired")
        return {"user": session.get("email"), "capabilities": ["sites", "benches", "servers"]}


class _MemStore:
    """Duck-typed fcctl SessionStore held in memory (real persistence is Platform Session)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    sid = property(lambda s: s.data.get("sid"))
    csrf = property(lambda s: s.data.get("csrf"))

    def set_central(self, **kw):
        self.data.update({k: v for k, v in kw.items() if v is not None})

    def save(self):
        pass


class RealAuth:
    """Thin wrapper over the installed fcctl package (imports guarded)."""

    def _central(self, session=None):
        try:
            from fcctl._sync.central import CentralClient
        except ImportError as e:
            raise FcAuthError(f"fcctl not installed: {e}")
        store = _MemStore(session)
        return CentralClient(store), store

    def send_code(self, email):
        c, _ = self._central()
        return c.call("POST", "central.api.auth.send_code", json={"email": email}, team=False)

    def verify_code(self, email, code):
        from fcctl import auth
        c, store = self._central()
        auth.login_with_code(c, email, lambda: code)
        return {"sid": store.sid, "csrf": store.csrf}

    def keepalive(self, session):
        import importlib
        import fcctl
        c, store = self._central(session)
        fn = getattr(fcctl, "keepalive", None)
        try:
            out = fn(c) if callable(fn) else importlib.import_module("fcctl.auth").keepalive(c)
        except Exception as e:
            if type(e).__name__ in ("AuthError", "ReauthFailed", "CsrfError"):
                raise FcAuthError(str(e))
            raise
        if store.sid and store.sid != session.get("sid"):
            session["sid"] = store.sid  # rotated sid; caller persists
        return out

    def whoami(self, session):
        c, _ = self._central(session)
        out = dict(self.keepalive(session) or {})
        out["capabilities"] = c.call("GET", "central.api.identity.get_capabilities")
        return out


def fc_backend():
    return FakeAuth() if frappe.conf.get("saas_core_fake_fcctl") else RealAuth()


def _fc_store():
    import importlib
    return importlib.import_module("saas_core.provisioning.stores").FrappeSessionStore()


def fc_load_session() -> dict | None:
    v = _fc_store().load(FC_LOGIN)
    return v.value if v else None


def fc_save_session(session: dict) -> None:
    s = _fc_store()
    cur = s.load(FC_LOGIN)
    s.save(FC_LOGIN, dict(session, saved_at=time.time()), cur.version if cur else 0)


def fc_is_paused() -> bool:
    try:
        return bool(frappe.cache().get_value(K_PAUSED))
    except Exception:
        return False


def fc_pause(reason: str) -> None:
    c = frappe.cache()
    c.set_value(K_PAUSED, 1)
    c.set_value(K_BANNER, "FC session expired: re-connect")
    c.set_value(K_LAST_ERR, reason)
    to = frappe.conf.get("saas_core_alert_email")
    send = getattr(frappe, "sendmail", None)
    if to and callable(send):
        try:
            from saas_core.notifications.templating import send as _tsend
            _tsend("fc_alert", [to], "[ops] Frappe Cloud sign-in expired: new sites are paused",
                   f"Provisioning is paused. Open Desk, go to FC Connect, and enter the sign-in code. "
                   f"Waiting jobs resume on their own. Reason: {reason}", {"reason": reason})
        except Exception:
            frappe.log_error("fc alert failed")


def fc_unpause() -> list:
    """Clear pause + banner and re-enqueue the jobs that were held pending."""
    c = frappe.cache()
    was = fc_is_paused()
    for k in (K_PAUSED, K_BANNER, K_LAST_ERR):
        c.delete_value(k)
    if not was:
        return []
    names = frappe.get_all("Provision Job", filters={"state": "pending"}, pluck="name")
    for n in names:
        frappe.enqueue(RUN_JOB, queue="long", timeout=3600, enqueue_after_commit=True, job=n)
    return names


def fc_send_code(email: str) -> dict:
    fc_backend().send_code(email)
    return {"sent": True, "email": email}


def fc_verify(email: str, code: str) -> dict:
    out = fc_backend().verify_code(email, code)
    fc_save_session({"sid": out["sid"], "csrf": out.get("csrf"), "email": email})
    frappe.cache().set_value(K_LAST_KA, time.time())
    return {"connected": True, "email": email, "requeued": len(fc_unpause())}


def _fc_reauth(sess: dict) -> dict | None:
    """Reauther chain: site_config saas_core_fc_reauther = dotted callable(session) -> new session | None."""
    import importlib
    path = frappe.conf.get("saas_core_fc_reauther")
    if not path:
        return None
    try:
        mod, fn = path.rsplit(".", 1)
        new = getattr(importlib.import_module(mod), fn)(sess) or None
    except Exception:
        frappe.log_error("fc reauther failed")
        return None
    if new and new.get("sid"):
        fc_save_session(new)  # every successful reauth is persisted here, whoever the caller is
    return new


def keepalive() -> dict:
    """Scheduler job (every 30 min): slide the FC session. AuthError -> reauther, else pause queue + banner + alert."""
    sess = fc_load_session()
    if not sess:
        return {"ok": False, "reason": "no_session"}
    try:
        fc_backend().keepalive(sess)
    except FcAuthError as e:
        new = _fc_reauth(sess)
        if new:
            fc_save_session(new)
            frappe.cache().set_value(K_LAST_KA, time.time())
            fc_unpause()
            return {"ok": True, "reauthed": True}
        fc_pause(str(e))
        return {"ok": False, "paused": True, "reason": str(e)}
    fc_save_session(sess)  # persists any rotated sid
    frappe.cache().set_value(K_LAST_KA, time.time())
    return {"ok": True}


def fc_status() -> dict:
    c = frappe.cache()
    sess, err = None, None
    try:
        sess = fc_load_session()
    except Exception as e:
        err = str(e)
    out = {"connected": bool(sess), "paused": fc_is_paused(), "banner": c.get_value(K_BANNER),
           "last_keepalive": c.get_value(K_LAST_KA), "last_error": c.get_value(K_LAST_ERR),
           "session_age_seconds": None, "whoami": None, "email": sess.get("email") if sess else None}
    if err:
        out["error"] = err
    if sess:
        if sess.get("saved_at"):
            out["session_age_seconds"] = int(time.time() - sess["saved_at"])
        try:
            out["whoami"] = fc_backend().whoami(sess)
        except FcAuthError as e:
            out["whoami_error"] = str(e)
    return out


def daily_tenant_backups():
    """Lazy guarded wrapper: runs provisioning.backups.run_daily_backups if that module exists."""
    import importlib
    try:
        mod = importlib.import_module("saas_core.provisioning.backups")
    except ImportError:
        return None
    return mod.run_daily_backups()
