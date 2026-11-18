"""Frappe-backed implementations of the provisioning ports, plus FcctlAdapter over the real fcctl package.

fcctl is imported lazily and ONLY here; everything else depends on ports.py.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
from typing import Any, Callable

import frappe

from saas_core.config import get_setting

audit = importlib.import_module("saas_core.audit")  # sys.modules lookup (survives test re-import)
from .ports import (APPROVALS_REQUIRED, APPROVAL_TTL_MINUTES, ApprovalError, CasConflict, LockBusy, ProvisionSpec,
                    RunResult, Versioned, check_transition, compute_digest)

# ---------------------------------------------------------------- sessions


def _fernet(key: str | bytes):
    from cryptography.fernet import Fernet  # lazy: only needed when sessions are used
    return Fernet(key if isinstance(key, bytes) else key.encode())


class FrappeSessionStore:
    """Platform Session doc (name = login). Blob is Fernet-encrypted JSON; version gives optimistic CAS."""

    def __init__(self, cipher: Any = None, lock_wait: float = 5.0):
        self._cipher = cipher
        self.lock_wait = lock_wait

    def _c(self):
        if self._cipher is None:
            key = get_setting("session_key")
            if not key:
                raise RuntimeError("site_config saas_core_session_key is not set")
            self._cipher = _fernet(key)
        return self._cipher

    @staticmethod
    def _blob(doc) -> str | None:
        gp = getattr(doc, "get_password", None)
        try:
            return gp("blob", raise_exception=False) if callable(gp) else doc.get("blob")
        except Exception:
            return doc.get("blob")

    def load(self, login: str) -> Versioned[dict] | None:
        if not frappe.db.exists("Platform Session", login):
            return None
        doc = frappe.get_doc("Platform Session", login)
        blob = self._blob(doc)
        if not blob:
            return None
        data = json.loads(self._c().decrypt(blob.encode() if isinstance(blob, str) else blob).decode())
        return Versioned(data, int(doc.get("version") or 0))

    def save(self, login: str, session: dict, expected_version: int) -> int:
        with audit.cache_lock(f"saas_core:session:cas:{login}", ttl=10, wait=self.lock_wait):
            exists = frappe.db.exists("Platform Session", login)
            doc = frappe.get_doc("Platform Session", login) if exists else None
            current = int(doc.get("version") or 0) if doc else 0
            if current != expected_version:
                raise CasConflict(f"{login}: expected v{expected_version}, found v{current}")
            blob = self._c().encrypt(json.dumps(session, sort_keys=True).encode()).decode()
            if doc is None:
                frappe.get_doc({"doctype": "Platform Session", "name": login, "login": login, "blob": blob,
                                "version": current + 1}).insert(ignore_permissions=True)
            else:
                doc.blob, doc.version = blob, current + 1
                doc.save(ignore_permissions=True)
            return current + 1

    @contextlib.contextmanager
    def lock(self, login: str, ttl: float):
        try:
            with audit.cache_lock(f"saas_core:session:lock:{login}", ttl=ttl, wait=self.lock_wait):
                yield
        except audit.LockTimeout as e:
            raise LockBusy(str(e))


# ---------------------------------------------------------------- approvals


class FrappeApprovalStore:
    """Approval Request doc. Two-person rule: approver != requester, role SaaS Admin, N distinct approvers."""

    def __init__(self, required: int = APPROVALS_REQUIRED, ttl_minutes: int = APPROVAL_TTL_MINUTES,
                 roles_for: Callable[[str], list[str]] | None = None):
        self.required, self.ttl_minutes = required, ttl_minutes
        self._roles_for = roles_for or (lambda u: frappe.get_roles(u))

    def request(self, op, target, args, requested_by, blast_radius=None) -> str:
        from frappe.utils import add_to_date, now_datetime
        doc = frappe.get_doc({"doctype": "Approval Request", "op": op, "target": target,
                              "args": json.dumps(args or {}, sort_keys=True, default=str),
                              "digest": compute_digest(op, target, args), "requested_by": requested_by,
                              "approved_by": json.dumps([]), "status": "pending",
                              "expires_at": add_to_date(now_datetime(), minutes=self.ttl_minutes),
                              "blast_radius": json.dumps(blast_radius or {}, default=str)})
        doc.insert(ignore_permissions=True)
        audit.log("approval.requested", actor=requested_by, target_doctype="Approval Request", target=doc.name,
                  details={"op": op, "target": target})
        return doc.name

    @staticmethod
    def _approvers(doc) -> list[str]:
        v = doc.get("approved_by")
        return list(json.loads(v) if isinstance(v, str) and v else (v or []))

    def _expired(self, doc) -> bool:
        from frappe.utils import get_datetime, now_datetime
        exp = doc.get("expires_at")
        return bool(exp) and get_datetime(exp) < now_datetime()

    def approve(self, name: str, approver: str) -> dict:
        doc = frappe.get_doc("Approval Request", name)
        if doc.get("status") != "pending":
            raise ApprovalError(f"approval is {doc.get('status')}")
        if self._expired(doc):
            doc.status = "expired"; doc.save(ignore_permissions=True)
            raise ApprovalError("approval expired")
        if approver == doc.get("requested_by"):
            raise ApprovalError("requester cannot approve their own request")
        if "SaaS Admin" not in (self._roles_for(approver) or []):
            raise ApprovalError("approver needs the SaaS Admin role")
        have = self._approvers(doc)
        if approver in have:
            raise ApprovalError("already approved by this user")
        have.append(approver)
        doc.approved_by = json.dumps(have)
        if len(have) >= self.required:
            doc.status = "approved"
        doc.save(ignore_permissions=True)
        audit.log("approval.approved", actor=approver, target_doctype="Approval Request", target=name,
                  details={"approvers": have})
        return {"name": name, "status": doc.status, "approved_by": have}

    def assert_executable(self, name: str, op: str, target: str, args: dict | None) -> dict:
        """Raise ApprovalError unless approved by enough people, unexpired and the digest still matches."""
        try:
            doc = frappe.get_doc("Approval Request", name)
        except frappe.DoesNotExistError:
            raise ApprovalError("approval not found")
        if doc.get("status") not in ("approved", "executing"):
            raise ApprovalError(f"approval is {doc.get('status')}")
        if doc.get("status") == "approved" and self._expired(doc):
            doc.status = "expired"; doc.save(ignore_permissions=True)
            raise ApprovalError("approval expired")
        approvers = set(self._approvers(doc)) - {doc.get("requested_by")}
        if len(approvers) < self.required:
            raise ApprovalError(f"needs {self.required} approvers")
        if doc.get("op") != op or doc.get("target") != target or doc.get("digest") != compute_digest(op, target, args):
            raise ApprovalError("approval does not match this operation (digest mismatch)")
        stored = doc.get("args")
        stored = json.loads(stored) if isinstance(stored, str) and stored else (stored or {})
        if compute_digest(doc.get("op"), doc.get("target"), stored) != doc.get("digest"):
            raise ApprovalError("approval record was altered (digest mismatch)")
        return {"name": name, "approved_by": sorted(approvers)}

    def mark(self, name: str, status: str, journal_id: str | None = None) -> None:
        doc = frappe.get_doc("Approval Request", name)
        doc.status = status
        if journal_id:
            doc.journal_id = journal_id
        doc.save(ignore_permissions=True)


# ---------------------------------------------------------------- journal


class JournalError(RuntimeError):
    pass


class FrappeJournal:
    """Provision Job Step rows on the Provision Job. begin() persists the step BEFORE any dispatch."""

    def _job(self, job: str):
        return frappe.get_doc("Provision Job", job)

    def begin(self, job: str, name: str, *, hook_version: str | None = None) -> int:
        from frappe.utils import now_datetime
        doc = self._job(job)
        seq = len(doc.get("steps") or []) + 1
        doc.append("steps", {"seq": seq, "step_name": name, "state": "pending", "hook_version": hook_version,
                             "started_at": now_datetime()})
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return seq

    def transition(self, job, seq, state, *, message=None, handle=None) -> None:
        from frappe.utils import now_datetime
        doc = self._job(job)
        row = next((r for r in (doc.get("steps") or []) if int(r.get("seq") or 0) == int(seq)), None)
        if row is None:
            doc.reload()
            row = next((r for r in (doc.get("steps") or []) if int(r.get("seq") or 0) == int(seq)), None)
        if row is None:
            raise JournalError(f"step seq={seq} not found on Provision Job {job}")
        check_transition(row.get("state") or "pending", state)
        row.set("state", state)
        if message is not None:
            row.set("message", message)
        if handle is not None:
            row.set("handle", handle)
        if state in ("succeeded", "failed", "needs_attention"):
            row.set("finished_at", now_datetime())
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    def steps(self, job: str) -> list[dict]:
        return [dict(r.as_dict()) if hasattr(r, "as_dict") else dict(r) for r in (self._job(job).get("steps") or [])]

    def set_job_state(self, job: str, state: str, *, error: str | None = None) -> None:
        from frappe.utils import now_datetime
        doc = self._job(job)
        check_transition(doc.get("state") or "pending", state)
        doc.state = state
        doc.needs_attention = 1 if state == "needs_attention" else 0
        if error is not None:
            doc.error = error
        if state in ("succeeded", "failed"):
            doc.finished_at = now_datetime()
        doc.save(ignore_permissions=True)
        frappe.db.commit()


class FrappeAuditSink:
    def log(self, action, *, actor=None, target_doctype=None, target=None, details=None) -> str:
        return audit.log(action, actor=actor, target_doctype=target_doctype, target=target, details=details)


# ---------------------------------------------------------------- real fcctl adapter


class _LazySiteAdmin:
    """Opens `client.sites.admin(server, site)` on first use (the site only exists mid-run); closed after the run."""

    def __init__(self, client, server, site):
        self._c, self._srv, self._site, self._admin = client, server, site, None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._admin is None:
            self._admin = self._c.sites.admin(self._srv, self._site)
        return getattr(self._admin, name)

    def close(self):
        a, self._admin = self._admin, None
        if a is not None:
            a.close()


def _health_callables(checks):
    """Template health checks are data ({name, path, expect, admin}); fcctl calls each as fn(api, site).

    Pilot's own site health always runs first; non-admin HTTP checks GET https://<site><path> and compare the
    status. Admin checks need a site session and are skipped here. None/empty -> fcctl's default check."""
    if not checks:
        return None
    import urllib.request, urllib.error

    def http(c):
        def run(api, site):
            try:
                with urllib.request.urlopen(f"https://{site}{c.get('path') or '/'}", timeout=30) as r:
                    st = r.status
            except urllib.error.HTTPError as e:
                st = e.code
            except Exception:
                st = None
            return st == int(c.get("expect") or 200)
        return run

    def retrying(fn, attempts=None, delay=None):
        # Lifting maintenance takes a few seconds to reach the web workers; a first 503 is not a failure.
        n = int(attempts or get_setting("fc_health_attempts", 12) or 12)
        d = float(delay if delay is not None else (get_setting("fc_health_delay", 10) or 10))
        def run(api, site):
            import time
            last = None
            for i in range(n):
                last = fn(api, site)
                if getattr(last, "ok", last):
                    return last
                if i < n - 1:
                    time.sleep(d)
            return last
        return run

    out = [retrying(lambda api, site: api.health(site))]
    for c in checks:
        if callable(c):
            out.append(retrying(c))
        elif isinstance(c, dict) and not c.get("admin"):
            out.append(retrying(http(c)))
    return out


def _recipe_hook_resolver():
    """HookResolver over every registered recipe's hooks() ('name@version' -> fn); None when there are none."""
    from fcctl._sync.workflows import HookResolver
    from saas_core.recipes import registry
    hooks = {}
    for r in registry.all().values():
        hooks.update(r.hooks() or {})
    return HookResolver(hooks) if hooks else None


def _require_experimental(feature):
    from saas_core.experimental import require
    require(feature)


def _experimental_enabled(feature):
    from saas_core.experimental import enabled
    return enabled(feature)


class FcctlAdapter:
    """FcctlPort over the real `fcctl.Client` facade (sites / servers / backups / workflows).

    `client` is injectable (tests / fakes); otherwise pass `fcctl_client=` (an fcctl.Client). The facade is
    server-scoped, so the server for a site comes from `server_for(site)`, the sites seen at provision time,
    or the `server` default.
    """
    UNVERIFIED = False  # checked by tests/test_stores.py::test_adapter_targets_exist_in_real_fcctl

    def __init__(self, client: Any = None, profile: str | None = None, server: str | None = None,
                 server_for: Any = None, journal: Any = None, fcctl_client: Any = None, bucket: Any = None):
        self._client = client or fcctl_client
        self.profile, self.server, self._server_for, self._journal = profile, server, server_for, journal
        self._sites: dict[str, str] = {}
        self._bucket = bucket
        self._uploads: dict[str, str] = {}

    def _c(self):
        if self._client is None:
            raise RuntimeError("FcctlAdapter needs client= (an fcctl.Client)")
        return self._client

    def _srv(self, site):
        srv = self._sites.get(site) or (self._server_for(site) if self._server_for else None) or self.server
        if not srv:
            raise RuntimeError(f"FcctlAdapter: cannot resolve server for site {site}")
        return srv

    @staticmethod
    def _res(run: Any) -> RunResult:
        if hasattr(run, "to_dict"):
            d = run.to_dict()
        elif isinstance(run, dict):
            d = dict(run)
        else:  # facade ops return raw API payloads / PendingApproval-like objects
            d = {"state": getattr(run, "state", None) or "succeeded", "data": run}
        state = d.get("state") or d.get("status") or "succeeded"
        state = {"success": "succeeded", "ok": "succeeded", "error": "failed"}.get(state, state)
        return RunResult(ok=state != "failed", state=state, site=d.get("site"), handle=d.get("workflow_id") or d.get("handle"),
                         message=d.get("message") or d.get("error") or "", data=d)

    def find_workflow(self, workflow_id):
        if self._journal is None:
            return None
        try:
            e = self._journal.get(workflow_id)
        except FileNotFoundError:
            return None
        return self._res({"state": e.get("state"), "site": (e.get("args") or {}).get("site"),
                          "workflow_id": workflow_id, "message": e.get("error") or ""})

    def _default_domain(self, server) -> str:
        from fcctl import sites as fc_sites
        r = fc_sites.wildcard_domains(self._c().pilot(server))
        doms = r.get("domains") if isinstance(r, dict) else r
        if not doms:
            raise RuntimeError("FC returned no wildcard domains; set saas_core_site_domain")
        return str(doms[0]).lstrip("*.").lstrip(".")

    def provision_site(self, spec: ProvisionSpec):
        from fcctl import BackupSource
        m = spec.manifest
        server = m.get("server") or self.server or spec.plan
        if not spec.domain and "." not in spec.site:      # bare subdomain: use FC's default (first Pilot wildcard) domain
            spec = dataclasses.replace(spec, site=f"{spec.site}.{self._default_domain(server)}")
        self._sites[spec.site] = server
        src = m.get("backup_source")  # template backup: {profile?, server, site, ts} or bucket source
        bucket_src = src if isinstance(src, dict) and src.get("bucket") else None
        if bucket_src:
            src = None  # restore-from-golden: create + install apps first, then backups.restore_bucket_source
        if isinstance(src, dict):
            src = BackupSource(src.get("profile") or self.profile, src["server"], src["site"], src["ts"])
        kw = dict(server=server, site=spec.site, site_apps=[a["app"] for a in m.get("apps", [])],
                  from_backup=src, config=spec.config, post_restore_hooks=tuple(spec.hooks or m.get("hooks", [])),
                  health_checks=_health_callables(m.get("health_checks")), workflow_id=spec.workflow_id,
                  # Tenant servers are shared: every customer site lives on the same bench, so the
                  # "empty bench" guard must be off. Set saas_core_fc_dedicated_bench=1 to require an empty bench.
                  allow_existing_sites=not bool(get_setting("fc_dedicated_bench", 0)))
        if self._journal is not None:
            kw["journal"] = self._journal
        resolver = _recipe_hook_resolver()
        if resolver is not None:
            kw["hook_resolver"] = resolver
        lazy = None
        if m.get("site_admin"):  # opt-in per template: hooks get ctx.site_admin (SiteAdminClient)
            lazy = kw["admin"] = _LazySiteAdmin(self._c(), server, spec.site)
        try:
            run = self._c().workflows.provision_site(**kw)
            run.wait()
        finally:
            if lazy is not None:
                lazy.close()
        res = self._res(run)
        if bucket_src and res.ok and res.state == "succeeded":
            from .backups import restore_bucket_source
            r = restore_bucket_source(self, spec.site, bucket_src, workflow_id=spec.workflow_id)
            if not r.ok:
                return r
        return res

    # -- bucket / upload / trust tier (UNVERIFIED against live FC; injectable for tests)
    def bucket_get(self, bucket, key) -> bytes:
        _require_experimental("restore")  # guessed Pilot routes, never run live
        if self._bucket is None:
            raise NotImplementedError("FcctlAdapter needs bucket= (object with get(bucket, key) -> bytes)")
        return self._bucket.get(bucket, key)

    def create_upload(self, site):
        _require_experimental("restore")  # guessed Pilot routes, never run live
        r = self._c().pilot(self._srv(site)).post("/uploads", {})
        uid = (r or {}).get("upload_id") or (r or {}).get("id")
        self._uploads[uid] = site
        return uid

    def put_upload_part(self, upload_id, name, data: bytes):
        _require_experimental("restore")  # guessed Pilot routes, never run live
        # TODO(real bench): chunked PUT; exact Pilot route to verify live
        site = self._uploads[upload_id]
        return self._c().pilot(self._srv(site)).put(f"/uploads/{upload_id}/{name}", data)

    def restore_from_upload(self, site, body, *, workflow_id=None):
        _require_experimental("restore")  # guessed Pilot routes, never run live
        kw = {"owned_by_workflow": workflow_id}
        if self._journal is not None:
            kw["journal"] = self._journal
        return self._res(self._c().backups.restore(self._srv(site), site, from_upload=dict(body), **kw))

    def trust_tier(self):
        if not _experimental_enabled("trust_tier_preflight"):
            return None  # response shape never captured live; preflight is skipped
        """Normalised FC trust tier {spending_limit, running_monthly, resources_used, ...} or None if unavailable."""
        try:
            from fcctl import billing
            raw = billing.trust_tier(self._c().central)
        except Exception:  # noqa: BLE001 - fcctl absent / not logged in: preflight is skipped
            return None
        if not isinstance(raw, dict):
            return None
        raw = raw.get("message", raw) if isinstance(raw.get("message", raw), dict) else raw
        prog = raw.get("progress") or {}
        out = {"current": raw.get("current"), "spending_limit": raw.get("spending_limit"),
               "running_monthly": raw.get("running_monthly"),
               "resources_used": prog.get("resources_used", raw.get("resources_used")),
               "resource_limit": prog.get("resource_limit", raw.get("resource_limit"))}
        return out

    def suspend_site(self, site, *, workflow_id=None):
        return self._res(self._c().sites.suspend(self._srv(site), site))

    def resume_site(self, site, *, workflow_id=None):
        return self._res(self._c().sites.resume(self._srv(site), site))

    def terminate_site(self, site, *, workflow_id=None):
        # Client facade drop: guarded (pending approval) unless this workflow owns the site (D11 ownership check)
        kw = {"owned_by_workflow": workflow_id}
        if self._journal is not None:
            kw["journal"] = self._journal
        return self._res(self._c().sites.drop(self._srv(site), site, **kw))

    def create_backup(self, site, *, workflow_id=None):
        from fcctl import backups
        return self._res(backups.create(self._c().pilot(self._srv(site)), site))

    def list_backups(self, site):
        from fcctl import backups
        r = backups.list_backups(self._c().pilot(self._srv(site)), site)
        return r if isinstance(r, list) else list((r or {}).get("backups") or (r or {}).get("items") or [])

    def restore_backup(self, site, *, workflow_id=None, backup=None):
        """Workflow-owned restore (approval already verified by the runner; fcctl re-checks D11 ownership)."""
        b = backup if isinstance(backup, dict) else {}
        kw = {"owned_by_workflow": workflow_id}
        if b.get("server") and b.get("site") and b.get("ts"):
            kw["from_site_backup"] = {k: b[k] for k in ("server", "site", "ts", "parts") if b.get(k)}
        else:
            kw["from_upload"] = b.get("upload_id") or backup
        if self._journal is not None:
            kw["journal"] = self._journal
        return self._res(self._c().backups.restore(self._srv(site), site, **kw))

    def backup_download_link(self, site, backup_id):
        return self._c().backups.download_links(self._srv(site), site, backup_id)

    def login_link(self, site):
        return self._c().sites.login_link(self._srv(site), site)

    def set_admin_password(self, site, password):
        admin = self._c().sites.admin(self._srv(site), site)
        try:
            admin.set_admin_password(password)
        finally:
            admin.close()

    def force_password_reset(self, site, user="Administrator"):
        admin = self._c().sites.admin(self._srv(site), site)
        try:
            admin.force_password_reset(user)
        finally:
            admin.close()

    def create_password_setup_link(self, site, user="Administrator"):
        admin = self._c().sites.admin(self._srv(site), site)
        try:
            return admin.create_password_setup_link(user)
        finally:
            admin.close()

    def set_site_config(self, site, config):
        """PATCH /sites/<site>/configuration (same route fcctl's core.set_config hook uses). Merges keys."""
        self._c().pilot(self._srv(site)).patch(f"/sites/{site}/configuration", dict(config))

    def change_plan(self, site, plan, *, workflow_id=None):
        from fcctl import DiskShrinkRefused
        try:
            return self._res(self._c().servers.resize(self._srv(site), plan))
        except DiskShrinkRefused as e:
            from saas_core.api.v1._common import ApiError
            raise ApiError("validation_error", str(e) or "Disk shrink is not supported", 422,
                           {"plan": "disk_shrink_not_supported"})
