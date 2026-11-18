"""Builds the real `fcctl.Client` for Frappe Cloud mode.

The FC session lives in the DB (Platform Session, Fernet-encrypted with saas_core_session_key), never in
~/.fcctl. An expired session is recovered by the configured reauther (site_config `saas_core_fc_reauther`,
e.g. saas_core.fc_reauth.imap_reauther). Errors here never contain session values or other secrets.

Guard/policy: the fcctl Client facade runs provisioning ops directly. Destructive ops (terminate) stay gated
twice: saas_core's runner demands an executable two-person FrappeApprovalStore approval, and fcctl's drop is
"guarded" unless the workflow owns the site (ownership journal below).
"""
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import frappe

from saas_core.config import get_setting


class FcConfigError(RuntimeError):
    """Missing/invalid FC configuration. Message names the site_config key only."""


class DbSessionStore:
    """Duck-typed fcctl SessionStore backed by saas_core's Platform Session (jobs.fc_load_session/save)."""
    profile_dir = None

    def __init__(self, jobs=None):
        self._jobs = jobs or importlib.import_module("saas_core.jobs")
        self.data: dict = dict(self._jobs.fc_load_session() or {})

    sid = property(lambda s: s.data.get("sid"))
    csrf = property(lambda s: s.data.get("csrf"))

    def set_central(self, sid=None, csrf=None, **extra):
        if sid:
            self.data["sid"] = sid
        if csrf:
            self.data["csrf"] = csrf
        self.data.update({k: v for k, v in extra.items() if v is not None})
        self.save()

    def reload(self):
        """Re-read the persisted session so a login done elsewhere (reauther, other worker) is seen at once."""
        self.data = dict(self._jobs.fc_load_session() or {})
        return self

    def save(self):
        if self.data.get("sid"):
            self._jobs.fc_save_session(self.data)


class ReauthBridge:
    """central.reauth hook: fcctl calls recover() on a 401/CSRF error; we run saas_core's reauther chain."""

    def __init__(self, store: DbSessionStore, jobs=None):
        self.store = store
        self._jobs = jobs or importlib.import_module("saas_core.jobs")
        self.pilot = None

    def recover(self, scope="central", error=None, server=None):
        from fcctl.request import ReauthFailed
        new = self._jobs._fc_reauth(dict(self.store.data))
        if not new or not new.get("sid"):
            raise ReauthFailed(f"FC session expired and the reauther could not renew it ({scope})")
        self.store.set_central(sid=new["sid"], csrf=new.get("csrf"), email=new.get("email"))  # persists too
        self.store.reload()
        return "reauther"

    def ensure_session(self):
        """Proactive login when nothing is stored (raises ReauthFailed if it cannot)."""
        if not self.store.reload().sid:
            self.recover(scope="central")


_GUEST_MARKERS = ("login to access", "is not whitelisted", "as a guest")


def is_guest_error(e, has_session=True) -> bool:
    """Frappe Cloud answers a missing/expired session with 403 PermissionError (as Guest), which fcctl maps to
    ApiError rather than AuthError. Duck-typed on purpose (no fcctl import)."""
    if type(e).__name__ != "ApiError" or getattr(e, "status", None) != 403:
        return False
    text = f"{e} {getattr(e, 'body', '')}".lower()
    if any(m in text for m in _GUEST_MARKERS):
        return True
    return "permissionerror" in text and not has_session


def install_guest_reauth(client, bridge: ReauthBridge) -> None:
    """Wrap central.call: no stored session -> log in first; guest-403 ApiError -> reauth and retry once."""
    central = client.central
    orig = central.call
    state = {"checked": False}

    def call(*a, **kw):
        if not state["checked"]:
            state["checked"] = True
            bridge.ensure_session()
        try:
            return orig(*a, **kw)
        except Exception as e:  # noqa: BLE001
            if kw.get("_retried") or not is_guest_error(e, bool(bridge.store.sid)):
                raise
            bridge.recover(scope="central", error=e)
            kw["_retried"] = True
            return orig(*a, **kw)

    central.call = call
    client.ensure_session = bridge.ensure_session


def require_config() -> tuple[str, str | None]:
    """(team, server). Raises FcConfigError naming the missing key, never a value."""
    team = get_setting("fc_team")
    if not team:
        raise FcConfigError("Frappe Cloud mode needs site_config saas_core_fc_team")
    if not get_setting("session_key"):
        raise FcConfigError("Frappe Cloud mode needs site_config saas_core_session_key")
    return str(team), (get_setting("fc_server") or None)


def journal_dir() -> Path:
    base = get_setting("fc_journal_dir")
    if base:
        return Path(base)
    gsp = getattr(frappe, "get_site_path", None)
    return Path(gsp("private", "fcctl", "journal")) if callable(gsp) else Path(tempfile.gettempdir()) / "saas_core_fcctl_journal"


def build_fcctl_journal():
    """File journal so FcctlAdapter.find_workflow / workflow ownership survive worker restarts."""
    from fcctl.journal import Journal
    return Journal(journal_dir())


def build_fcctl_client():
    """A real fcctl.Client wired to the DB session, the configured team and the reauther chain."""
    team, _ = require_config()
    from fcctl import Client
    store = DbSessionStore()
    client = Client(store, team=team)
    bridge = ReauthBridge(store)
    client.central.reauth = bridge
    install_guest_reauth(client, bridge)
    return client


def fc_check() -> dict:
    """Secret-free connectivity diagnostic: whoami + wildcard domains (reauth happens transparently)."""
    team, server = require_config()
    out = {"authenticated": False, "email": None, "team": team, "server": server, "wildcard_domains": []}
    client = build_fcctl_client()
    try:
        from fcctl import auth, sites
        client.ensure_session()
        prof = auth.my_profile(client.central) or {}
        prof = prof.get("message", prof) if isinstance(prof.get("message", prof), dict) else prof
        out["authenticated"] = True
        out["email"] = prof.get("email") or prof.get("user") or prof.get("name")
        if server:
            r = sites.wildcard_domains(client.pilot(server))
            doms = r.get("domains") if isinstance(r, dict) else r
            out["wildcard_domains"] = [str(d) for d in (doms or [])]
    except Exception as e:  # noqa: BLE001 - report the class only; messages may embed hosts/tokens
        out["error"] = type(e).__name__
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    out["template_warnings"] = _template_warnings()
    return out


def _template_warnings() -> dict:
    """slug -> warnings for every Template (advisory; never raises)."""
    res = {}
    try:
        import frappe
        from .templates import TemplateError, validate_template
        for n in frappe.get_all("Template", pluck="name"):
            try:
                w = validate_template(frappe.get_doc("Template", n)).get("warnings")
            except TemplateError as e:
                w = [f"INVALID: {e}"]
            if w:
                res[n] = w
    except Exception:  # noqa: BLE001
        pass
    return res
